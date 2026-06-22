Based on my analysis of the code, this is a real vulnerability.

---

### Title
Permanent Deposit Skip via Log-Flood Attack in `scrape_block_range` — (`rs/ethereum/cketh/minter/src/deposit.rs`)

### Summary

An unprivileged attacker can permanently cause a targeted high-value deposit to never be minted by flooding the same Ethereum block with enough small deposit events to exceed the HTTP outcall response size limit. When the binary-splitting logic in `scrape_block_range` reaches `from_block == to_block` and the response is still too large, the block is unconditionally and permanently skipped via `SkippedBlockForContract`, with no retry or recovery path.

### Finding Description

`scrape_block_range` in [1](#0-0)  implements a binary-splitting retry loop. When `eth_getLogs` returns a "response too large" error:

- If `from_block != to_block`: the range is split into halves and re-queued (lines 285–295).
- If `from_block == to_block`: the block is **permanently skipped** via `SkippedBlockForContract` and `last_scraped_block_number` is advanced past it. [2](#0-1) 

The initial HTTP outcall response size budget is only `ETH_GET_LOGS_INITIAL_RESPONSE_SIZE_ESTIMATE + HEADER_SIZE_LIMIT = 100 + 2048 = 2148 bytes`. [3](#0-2) 

There is no logic to retry the same single-block query with a larger response size estimate. Once the block is skipped, `apply_state_transition` records it permanently: [4](#0-3) 

The `SkippedBlockForContract` event type is a terminal state with no recovery mechanism in the event log: [5](#0-4) 

### Impact Explanation

Any deposit whose Ethereum block is skipped is **permanently lost** — the minter advances `last_scraped_block_number` past it and never re-queries it. The victim's ckETH or ckERC20 tokens are never minted. This constitutes targeted theft: the attacker can select a specific victim transaction (e.g., a large ETH deposit visible in the mempool) and cause it to be silently dropped.

### Likelihood Explanation

The attack requires:
1. Monitoring the Ethereum mempool for a large deposit.
2. Front-running it by submitting enough small deposits to the same helper contract in the same block to inflate the `eth_getLogs` response beyond the IC HTTP outcall size limit.
3. Placing the attacker's own deposits in adjacent blocks (not the flooded block), so only the victim's block is skipped.

The cost is non-trivial (Ethereum gas for many transactions) but economically rational for a sufficiently large victim deposit. The attack is fully permissionless — no privileged access is required.

### Recommendation

1. **Retry with a larger response size estimate** before skipping a single block. If `from_block == to_block` and the response is too large, retry the same query with a progressively larger `response_size_estimate` up to the IC's hard maximum (~2 MB) before emitting `SkippedBlockForContract`.
2. **Add a recovery mechanism** for skipped blocks — e.g., a governance-callable function that re-scrapes a specific block, or an operator alert that triggers manual intervention.
3. **Bound the skip condition** more tightly: only skip if the response size exceeds the IC's absolute hard limit, not the initial low estimate.

### Proof of Concept

State-machine test:
1. Configure the minter with a low `ETH_GET_LOGS_INITIAL_RESPONSE_SIZE_ESTIMATE`.
2. Simulate a block containing N+1 deposit log entries where N entries cause `is_response_too_large` to return true.
3. Place a high-value deposit as one of those N+1 entries.
4. Run `scrape_block_range` against this simulated block.
5. Observe that `SkippedBlockForContract` is emitted for that block and `last_scraped_block_number` advances past it.
6. Verify that no `AcceptedDeposit` event is ever emitted for the high-value deposit — it is permanently lost.

The binary-splitting loop will reduce the range to `from_block == to_block` for the flooded block, hit the size limit again, and unconditionally skip it at: [6](#0-5)

### Citations

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L235-309)
```rust
async fn scrape_block_range<S>(
    rpc_client: &EvmRpcClient<IcRuntime, CandidResponseConverter, DoubleCycles>,
    contract_address: Address,
    topics: Vec<Topic>,
    block_range: BlockRangeInclusive,
) -> Result<(), MultiCallError<Vec<LogEntry>>>
where
    S: LogScraping,
{
    let mut subranges = VecDeque::new();
    subranges.push_back(block_range);

    while !subranges.is_empty() {
        let range = subranges.pop_front().unwrap();
        let (from_block, to_block) = range.clone().into_inner();

        let result = rpc_client
            .get_logs(vec![contract_address.into_bytes()])
            .with_from_block(from_block)
            .with_to_block(to_block)
            .with_topics(into_evm_topic(topics.clone()))
            .with_cycles(MIN_ATTACHED_CYCLES)
            .with_response_size_estimate(
                ETH_GET_LOGS_INITIAL_RESPONSE_SIZE_ESTIMATE + HEADER_SIZE_LIMIT,
            )
            .try_send()
            .await
            .reduce_with_strategy(NoReduction)
            .map(<S::Parser>::parse_all_logs);

        match result {
            Ok((events, errors)) => {
                register_deposit_events(S::ID, events, errors);
                mutate_state(|s| S::update_last_scraped_block_number(s, to_block));
            }
            Err(e) => {
                log!(INFO, "Failed to get {} logs in range {range}: {e:?}", S::ID);
                if e.has_http_outcall_error_matching(is_response_too_large) {
                    if from_block == to_block {
                        mutate_state(|s| {
                            process_event(
                                s,
                                EventType::SkippedBlockForContract {
                                    contract_address,
                                    block_number: to_block,
                                },
                            );
                        });
                        mutate_state(|s| S::update_last_scraped_block_number(s, to_block));
                    } else {
                        let (left_half, right_half) = range.partition_into_halves();
                        if let Some(r) = right_half {
                            let upper_range = subranges
                                .pop_front()
                                .map(|current_next| r.clone().join_with(current_next))
                                .unwrap_or(r);
                            subranges.push_front(upper_range);
                        }
                        if let Some(lower_range) = left_half {
                            subranges.push_front(lower_range);
                        }
                        log!(
                            INFO,
                            "Too many logs received. Will retry with ranges {subranges:?}"
                        );
                    }
                } else {
                    log!(INFO, "Failed to get {} logs in range {range}: {e:?}", S::ID);
                    return Err(e);
                }
            }
        }
    }
    Ok(())
}
```

**File:** rs/ethereum/cketh/minter/src/eth_rpc_client/mod.rs (L24-26)
```rust
pub const HEADER_SIZE_LIMIT: u64 = 2 * 1024;
// We expect most of the calls to contain zero events.
pub const ETH_GET_LOGS_INITIAL_RESPONSE_SIZE_ESTIMATE: u64 = 100;
```

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L120-125)
```rust
        EventType::SkippedBlockForContract {
            contract_address,
            block_number,
        } => {
            state.record_skipped_block_for_contract(*contract_address, *block_number);
        }
```

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L159-166)
```rust
    /// Skipped block for a specific helper contract.
    #[n(23)]
    SkippedBlockForContract {
        #[n(0)]
        contract_address: Address,
        #[n(1)]
        block_number: BlockNumber,
    },
```
