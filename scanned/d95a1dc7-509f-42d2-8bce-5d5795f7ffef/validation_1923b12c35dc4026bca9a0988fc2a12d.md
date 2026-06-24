### Title
ckETH Minter Permanently Skips Blocks With Excessive Deposit Logs, Causing Irrecoverable ETH/ERC-20 Deposit Loss - (File: `rs/ethereum/cketh/minter/src/deposit.rs`)

---

### Summary

The `scrape_block_range` function in the ckETH minter permanently skips Ethereum blocks when the JSON-RPC log response is too large to process. Once a block is skipped, the minter advances its `last_scraped_block_number` past it and never revisits it. There is no automated recovery mechanism. All ETH or ERC-20 deposits made in a skipped block are permanently unprocessed — the assets remain locked in the Ethereum helper contract while no ckETH/ckERC-20 is ever minted.

---

### Finding Description

In `rs/ethereum/cketh/minter/src/deposit.rs`, the `scrape_block_range` function handles oversized JSON-RPC responses by binary-splitting the block range and retrying each half. When the range is narrowed to a single block (`from_block == to_block`) and the response is still too large, the code takes the following path:

```rust
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
``` [1](#0-0) 

The call to `S::update_last_scraped_block_number(s, to_block)` permanently advances the scraping cursor past the skipped block. Future invocations of `scrape_until_block` will start from `last_scraped_block_number + 1`, so the skipped block is never re-examined. [2](#0-1) 

The `SkippedBlockForContract` event is handled in `apply_state_transition` by calling `record_skipped_block_for_contract`, which only inserts the block number into a `skipped_blocks` tracking map — there is no re-processing path. [3](#0-2) 

```rust
pub fn record_skipped_block_for_contract(
    &mut self,
    contract_address: Address,
    block_number: BlockNumber,
) {
    let entry = self.skipped_blocks.entry(contract_address).or_default();
    assert!(
        entry.insert(block_number),
        "BUG: block {block_number} was already skipped for contract {contract_address}",
    );
}
``` [4](#0-3) 

The `EventType::SkippedBlockForContract` variant is explicitly documented in the event log: [5](#0-4) 

This affects all three scraping pipelines (`ReceivedEthLogScraping`, `ReceivedErc20LogScraping`, `ReceivedEthOrErc20LogScraping`) since all three call the same `scrape_block_range` generic function: [6](#0-5) 

The analogous defensive mechanism in the ckBTC minter (`CleanButMintUnknown`) and the ckETH minter's `QuarantinedDeposit` both explicitly document that they "will not be processed without further manual intervention": [7](#0-6) [8](#0-7) 

---

### Impact Explanation

Any user who called `depositEth` or `depositErc20` on the Ethereum helper contract in a skipped block will never receive ckETH or ckERC-20. Their assets are permanently locked in the helper contract. The minter has no endpoint, governance proposal path, or timer task that re-processes entries in `skipped_blocks`. The loss is silent — the user receives no error and no refund.

---

### Likelihood Explanation

An unprivileged Ethereum user (chain-fusion user) can deliberately trigger this condition by submitting a large number of `depositEth` calls within a single Ethereum block, flooding the helper contract's event log for that block. When the minter's HTTP outcall to the JSON-RPC provider returns a response exceeding `ETH_GET_LOGS_INITIAL_RESPONSE_SIZE_ESTIMATE + HEADER_SIZE_LIMIT`, the binary-split retry loop will eventually reach a single-block range that still exceeds the limit, triggering the skip. Legitimate co-depositors in the same block lose their funds. This can also occur organically during periods of high protocol activity without any attacker involvement. [9](#0-8) 

---

### Recommendation

1. **Implement a recovery path for skipped blocks**: Add a privileged endpoint (callable via NNS governance proposal) that re-scrapes a specific block number for a given contract address, bypassing the `last_scraped_block_number` cursor.
2. **Raise a critical alert**: When `SkippedBlockForContract` is emitted, increment a critical error counter (analogous to `critical_error_induct_response_failed` in message routing) so operators are immediately notified.
3. **Avoid advancing the cursor on skip**: Instead of calling `S::update_last_scraped_block_number(s, to_block)` after a skip, leave the cursor behind the skipped block and record the skip separately, so a future retry with a larger response budget or a different RPC provider can still attempt the block.

---

### Proof of Concept

1. Attacker submits N `depositEth(amount, principal, subaccount)` calls to the Ethereum helper contract within a single block, where N is large enough that `eth_getLogs` for that single block returns a response exceeding the minter's size limit.
2. The ckETH minter's periodic `scrape_logs` timer fires and calls `scrape_until_block::<ReceivedEthLogScraping>`.
3. `scrape_block_range` binary-splits the range until `from_block == to_block` equals the attacker's block.
4. The single-block `eth_getLogs` response is still too large; `is_response_too_large` returns `true`.
5. `EventType::SkippedBlockForContract { contract_address, block_number }` is emitted and `last_scraped_block_number` is advanced to the attacker's block number.
6. All subsequent scraping starts from `attacker_block + 1`. The attacker's block — and any legitimate user deposits it contained — is never processed again.
7. Legitimate users who deposited in that block never receive ckETH; their ETH is permanently locked in the helper contract. [10](#0-9)

### Citations

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L156-158)
```rust
    scrape_until_block::<ReceivedEthLogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedErc20LogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedEthOrErc20LogScraping>(last_block_number, max_block_spread).await;
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L200-204)
```rust
    let block_range = BlockRangeInclusive::new(
        scrape
            .last_scraped_block_number
            .checked_increment()
            .unwrap_or(BlockNumber::MAX),
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L251-263)
```rust
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
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L265-308)
```rust
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

**File:** rs/ethereum/cketh/minter/src/state.rs (L386-396)
```rust
    pub fn record_skipped_block_for_contract(
        &mut self,
        contract_address: Address,
        block_number: BlockNumber,
    ) {
        let entry = self.skipped_blocks.entry(contract_address).or_default();
        assert!(
            entry.insert(block_number),
            "BUG: block {block_number} was already skipped for contract {contract_address}",
        );
    }
```

**File:** rs/ethereum/cketh/minter/src/state/event.rs (L141-149)
```rust
    /// The minter unexpectedly panic while processing a deposit.
    /// The deposit is quarantined to prevent any double minting and
    /// will not be processed without further manual intervention.
    #[n(21)]
    QuarantinedDeposit {
        /// The unique identifier of the deposit on the Ethereum network.
        #[n(0)]
        event_source: EventSource,
    },
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L327-337)
```rust
        // After the call to `mint_ckbtc` returns, in a very unlikely situation the
        // execution may panic/trap without persisting state changes and then we will
        // have no idea whether the mint actually succeeded or not. If this happens
        // the use of the guard below will help set the utxo to `CleanButMintUnknown`
        // status so that it will not be minted again. Utxos with this status will
        // require manual intervention.
        let guard = scopeguard::guard((utxo.clone(), caller_account), |(utxo, account)| {
            mutate_state(|s| {
                state::audit::mark_utxo_checked_mint_unknown(s, utxo, account, runtime)
            });
        });
```
