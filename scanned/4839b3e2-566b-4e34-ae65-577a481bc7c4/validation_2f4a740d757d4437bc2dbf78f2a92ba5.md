### Title
Single Misbehaving External RPC Provider Can DOS ckETH/ckERC20 Deposit Scraping Process - (`File: rs/ethereum/cketh/minter/src/deposit.rs`)

### Summary

The ckETH minter's log-scraping pipeline uses a `NoReduction` consensus strategy when querying multiple Ethereum JSON-RPC providers via the EVM RPC canister. If any provider returns data inconsistent with the others, the entire block-range scraping halts and no new deposits (ckETH or ckERC20 mints) are processed until the provider recovers or is replaced via a governance upgrade. This is a direct IC analog to M-7: a single external protocol failure DOSes the entire process.

### Finding Description

`scrape_logs()` in `rs/ethereum/cketh/minter/src/deposit.rs` runs three sequential log-scraping passes:

```
scrape_until_block::<ReceivedEthLogScraping>(...)
scrape_until_block::<ReceivedErc20LogScraping>(...)
scrape_until_block::<ReceivedEthOrErc20LogScraping>(...)
``` [1](#0-0) 

Inside `scrape_until_block`, each block-range chunk is passed to `scrape_block_range`, which calls the EVM RPC canister and applies `NoReduction`:

```rust
.reduce_with_strategy(NoReduction)
``` [2](#0-1) 

`NoReduction` is defined to return `Err(MultiCallError::InconsistentResults(...))` whenever providers disagree — even if a supermajority agrees:

```rust
impl<T> ReductionStrategy<T> for NoReduction {
    fn reduce(&self, results: EvmMultiRpcResult<T>) -> Result<T, MultiCallError<T>> {
        consistent_result_or_reduce(results, |inconsistent| {
            Err(MultiCallError::InconsistentResults(inconsistent))
        })
    }
}
``` [3](#0-2) 

When `scrape_block_range` returns this error (for any reason other than response-too-large), `scrape_until_block` immediately returns, abandoning all remaining block ranges for that scraping type:

```rust
Err(e) => {
    log!(...);
    return;
}
``` [4](#0-3) 

The EVM RPC client is configured with a 3-of-4 threshold strategy for mainnet:

```rust
.with_consensus_strategy(ConsensusStrategy::Threshold {
    total: Some(TOTAL_NUMBER_OF_PROVIDERS),
    min: min_threshold,  // 3 for mainnet
})
``` [5](#0-4) 

However, the EVM RPC canister still returns `EvmMultiRpcResult::Inconsistent` when providers disagree (even if 3 agree), and `NoReduction` converts any `Inconsistent` result into an error regardless of how many providers agreed. A 2-2 provider split, or a single provider returning wrong data that breaks the 3-of-4 threshold, halts scraping entirely.

### Impact Explanation

When scraping halts:
- No new ETH or ERC20 deposit events are detected from the Ethereum blockchain.
- No ckETH or ckERC20 tokens are minted for users who have already sent ETH/ERC20 to the helper contract.
- User funds are locked on Ethereum with no corresponding IC-side credit until the provider issue is resolved via a governance upgrade.
- Withdrawals that depend on `finalized_transaction_count()` (also using `NoReduction`) are similarly blocked.

This is confirmed by three documented mainnet incidents requiring emergency governance upgrades:
- Cloudflare returning wrong logs after the Dencun upgrade → minting stuck.
- Ankr dropping IPv6 → minter stuck.
- LlamaNodes down + Pocket Network consensus failures → minter stuck. [6](#0-5) [7](#0-6) [8](#0-7) 

### Likelihood Explanation

**High.** This has already occurred multiple times in production. The root cause is structural: any single provider that returns data inconsistent with the others (due to bugs, pauses, network issues, API changes, or deliberate manipulation) triggers the halt. The minter integrates with 4 external providers, each of which is an independent point of failure. The 3-of-4 threshold upgrade reduced frequency but did not eliminate the issue — a 2-2 split or any provider returning data that prevents threshold consensus still causes a halt. The attacker-controlled entry path is a "canister HTTP participant" (an external RPC provider) returning divergent responses to different IC replicas, which is a realistic and observed failure mode.

### Recommendation

1. **Replace `NoReduction` with a majority-tolerant strategy** (e.g., `StrictMajorityByKey` or `Threshold`) for `scrape_block_range` and `finalized_transaction_count`. If 3-of-4 providers agree, accept the majority result rather than requiring unanimity.
2. **Implement per-provider circuit-breaking**: track providers that consistently return inconsistent results and temporarily exclude them from the quorum, continuing scraping with the remaining providers.
3. **Decouple scraping failure from scraping halt**: on a non-size-related error, log the failure and advance the scraped block pointer by a configurable amount rather than retrying the same range indefinitely, to avoid permanent stalls.

### Proof of Concept

The existing test `should_not_mint_when_logs_too_inconsistent` demonstrates the halt with a 2-2 provider split:

```rust
mock.respond_with(JsonRpcProvider::Provider1, block_pi_logs.clone())
    .respond_with(JsonRpcProvider::Provider2, public_node_logs.clone())
    .respond_with(JsonRpcProvider::Provider3, block_pi_logs.clone())
    .respond_with(JsonRpcProvider::Provider4, public_node_logs.clone())
``` [9](#0-8) 

With a 2-2 split, `NoReduction` returns `InconsistentResults`, `scrape_until_block` returns early, and no mint occurs. The `should_retry_from_same_block_when_scrapping_fails` test further confirms that on any non-size error, the scraper stops at the current block and does not advance: [10](#0-9) 

The `SyncedToBlock` event remains at `LAST_SCRAPED_BLOCK_NUMBER_AT_INSTALL` (unchanged), confirming the DOS. The three mainnet emergency upgrades are the real-world proof of exploitability.

### Citations

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L140-159)
```rust
pub async fn scrape_logs() {
    let _guard = match TimerGuard::new(TaskType::ScrapEthLogs) {
        Ok(guard) => guard,
        Err(_) => return,
    };
    let last_block_number = match update_last_observed_block_number().await {
        Some(block_number) => block_number,
        None => {
            log!(
                DEBUG,
                "[scrape_logs]: skipping scrapping logs: no last observed block number"
            );
            return;
        }
    };
    let max_block_spread = read_state(|s| s.max_block_spread_for_logs_scraping());
    scrape_until_block::<ReceivedEthLogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedErc20LogScraping>(last_block_number, max_block_spread).await;
    scrape_until_block::<ReceivedEthOrErc20LogScraping>(last_block_number, max_block_spread).await;
}
```

**File:** rs/ethereum/cketh/minter/src/deposit.rs (L222-231)
```rust
            Ok(()) => {}
            Err(e) => {
                log!(
                    INFO,
                    "[scrape_contract_logs]: Failed to scrape {} logs in range {block_range}: {e:?}",
                    S::ID
                );
                return;
            }
        }
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

**File:** rs/ethereum/cketh/minter/src/eth_rpc_client/mod.rs (L56-63)
```rust
    EvmRpcClient::builder(IcRuntime::new(), evm_rpc_id)
        .with_rpc_sources(providers)
        .with_consensus_strategy(ConsensusStrategy::Threshold {
            total: Some(TOTAL_NUMBER_OF_PROVIDERS),
            min: min_threshold,
        })
        .with_retry_strategy(DoubleCycles::with_max_num_retries(MAX_NUM_RETRIES))
        .build()
```

**File:** rs/ethereum/cketh/minter/src/eth_rpc_client/mod.rs (L175-183)
```rust
pub struct NoReduction;

impl<T> ReductionStrategy<T> for NoReduction {
    fn reduce(&self, results: EvmMultiRpcResult<T>) -> Result<T, MultiCallError<T>> {
        consistent_result_or_reduce(results, |inconsistent| {
            Err(MultiCallError::InconsistentResults(inconsistent))
        })
    }
}
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_03_18.md (L14-16)
```markdown

Since the rollout of the Ethereum Dencun upgrade on 2024-03-13, Cloudflare, one of the 3 Ethereum JSON-RPC providers that the ckETH minter uses to interact with the Ethereum blockchain, returns wrong results (see examples below). As a consequence, the minting of ckETH is currently stuck and withdrawals are wrongly considered not finalized. This upgrade switches the minter to use Llama Nodes (`https://eth.llamarpc.com`) instead of Cloudflare as a third JSON-RPC provider (in addition to Ankr and Public Node).

```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_09_11.md (L14-17)
```markdown
The Ethereum JSON-RPC provider Ankr (`rpc.ankr.com`) recently dropped its IPv6 connectivity and will need according to its support team a month to fix it.
This resulted in the ckETH minter being stuck and unable to process deposits nor withdrawals.
As a temporary fix, this proposal replaces Ankr by another provider: `eth-pokt.nodies.app` from [Pocket Network](https://www.pokt.network/).
The long term solution is to use a more robust strategy (e.g., agreement among 3 providers, when 4 were queried) using the EVM-RPC canister.
```

**File:** rs/ethereum/cketh/mainnet/minter_upgrade_2024_09_12.md (L14-20)
```markdown

The ckETH minter is currently unable to process conversions between ETH/ERC20 and ckETH/ckERC20 due to the following events:
1. Proposal [132415](https://dashboard.internetcomputer.org/proposal/132415) was executed at 2024.09.11 09:28 (UTC) and successfully replaced the Ethereum JSON-RPC provider Ankr (rpc.ankr.com) with `eth-pokt.nodies.app` from [Pocket Network](https://www.pokt.network/).
2. Unfortunately, at the same time the Ethereum JSON-RPC provider LlamaNodes  `eth.llamarpc.com` was down and constantly replying with `no response`. This seems to have been resolved since the ckETH minter did make progress around 2024.09.11 22:00 (UTC) but stopped since then.
3. The [logs](https://sv3dd-oaaaa-aaaar-qacoa-cai.raw.icp0.io/logs?sort=desc) show that responses from the Ethereum JSON-RPC provider Pocket Network (`eth-pokt.nodies.app`) differ between the replicas resulting in consensus failures.

As a temporary fix, this proposal replaces the Ethereum JSON-RPC provider Pocket Network (`eth-pokt.nodies.app`) with the Ethereum JSON-RPC provider BlockPi (`https://ethereum.blockpi.network/v1/rpc/public).
```

**File:** rs/ethereum/cketh/minter/tests/cketh.rs (L241-264)
```rust
#[test]
fn should_not_mint_when_logs_too_inconsistent() {
    let deposit_params = DepositCkEthParams::default();
    let (block_pi_logs, public_node_logs) = {
        let block_pi_log_entry = deposit_params.to_log_entry();
        let llama_nodes_log_entry = DepositCkEthParams {
            amount: deposit_params.amount + 1,
            ..deposit_params.clone()
        }
        .to_log_entry();
        (vec![block_pi_log_entry], vec![llama_nodes_log_entry])
    };
    assert_ne!(block_pi_logs, public_node_logs);

    CkEthSetup::default()
        .deposit(deposit_params)
        .with_mock_eth_get_logs(move |mock| {
            mock.respond_with(JsonRpcProvider::Provider1, block_pi_logs.clone())
                .respond_with(JsonRpcProvider::Provider2, public_node_logs.clone())
                .respond_with(JsonRpcProvider::Provider3, block_pi_logs.clone())
                .respond_with(JsonRpcProvider::Provider4, public_node_logs.clone())
        })
        .expect_no_mint();
}
```

**File:** rs/ethereum/cketh/minter/tests/cketh.rs (L754-787)
```rust
#[test]
fn should_retry_from_same_block_when_scrapping_fails() {
    let cketh = CkEthSetup::default();
    let max_eth_logs_block_range = cketh.max_logs_block_range();
    let prev_events_len = cketh.get_all_events().len();

    cketh.env.advance_time(SCRAPING_ETH_LOGS_INTERVAL);
    MockJsonRpcProviders::when(JsonRpcMethod::EthGetBlockByNumber)
        .respond_for_all_with(block_response(DEFAULT_BLOCK_NUMBER))
        .build()
        .expect_rpc_calls(&cketh);
    let from_block = BlockNumber::from(LAST_SCRAPED_BLOCK_NUMBER_AT_INSTALL + 1);
    let to_block = from_block
        .checked_add(BlockNumber::from(max_eth_logs_block_range))
        .unwrap();
    MockJsonRpcProviders::when(JsonRpcMethod::EthGetLogs)
        .with_request_params(json!([{
            "fromBlock": from_block,
            "toBlock": to_block,
            "address": [ETH_HELPER_CONTRACT_ADDRESS],
            "topics": [cketh.received_eth_event_topic()]
        }]))
        .respond_for_all_with(empty_logs())
        .respond_for_providers_with([JsonRpcProvider::Provider2, JsonRpcProvider::Provider4], json!({"error":{"code":-32000,"message":"max message response size exceed"},"id":74,"jsonrpc":"2.0"}))
        .build()
        .expect_rpc_calls(&cketh);

    let cketh = cketh
        .check_audit_logs_and_upgrade(Default::default())
        .check_events()
        .skip(prev_events_len)
        .assert_has_unique_events_in_order(&[EventPayload::SyncedToBlock {
            block_number: LAST_SCRAPED_BLOCK_NUMBER_AT_INSTALL.into(),
        }]);
```
