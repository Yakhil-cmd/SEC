### Title
Single Failed `get_transaction_receipt` Call in `finalize_transactions_batch` Blocks All Pending ckETH/ckERC20 Withdrawal Finalizations — (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

---

### Summary

In the ckETH minter, `finalize_transactions_batch` fetches receipts for all pending transactions in parallel via `join_all`, but if **any single** receipt fetch returns `Err`, the function immediately `return`s at line 442, discarding all other receipts already fetched. This temporarily blocks every pending ckETH/ckERC20 withdrawal from being finalized in that timer tick — a direct IC analog of the EigenLayer M-02 pattern where one failing item in a batch blocks all others.

---

### Finding Description

`finalize_transactions_batch` in `rs/ethereum/cketh/minter/src/withdraw.rs` operates as follows:

1. It calls `sent_transactions_to_finalize` to collect all transaction hashes (including resubmissions) for nonces below the on-chain finalized count. Each hash maps to a `LedgerBurnIndex`.
2. It fires all `get_transaction_receipt` calls in parallel via `join_all`.
3. It iterates over the results. On `Ok(Some(receipt))` it records the receipt; on `Ok(None)` it logs and continues; on **`Err`** it immediately `return`s. [1](#0-0) 

The `return` at line 442 exits the entire `finalize_transactions_batch` function. Any receipts already successfully fetched for other withdrawal IDs in the same `join_all` batch are silently discarded. No state is updated. All withdrawals that were ready to finalize must wait for the next timer tick.

The `NoReduction` strategy used for `get_transaction_receipt` is the strictest available: it fails with `Err(MultiCallError::InconsistentResults(...))` whenever any provider disagrees with the others, not just when all providers fail. [2](#0-1) [3](#0-2) 

By contrast, `send_transactions_batch` uses `AnyOf` (succeeds if at least one provider agrees) and handles errors per-item without aborting the loop: [4](#0-3) 

There is also a secondary risk: after the loop, an `assert_eq!` compares `expected_finalized_withdrawal_ids` (all unique `LedgerBurnIndex` values from `txs_to_finalize`) against `actual_finalized_withdrawal_ids` (those that received receipts). If all transactions for a given nonce return `Ok(None)` — possible when the EVM RPC reports a finalized count that is ahead of what providers can serve receipts for — this assert **panics**, trapping the canister. [5](#0-4) 

The `sent_transactions_to_finalize` function confirms that for each nonce below the finalized count, all resubmitted transaction hashes are included, all mapping to the same `LedgerBurnIndex`: [6](#0-5) 

---

### Impact Explanation

Every ckETH and ckERC20 withdrawal that has reached the `sent_tx` state and whose nonce is below the on-chain finalized count is blocked from completing in the affected timer tick. Users experience delayed withdrawals. The delay is bounded by the timer retry interval, making this a **temporary DoS** — directly analogous to the EigenLayer M-02 finding where the judge accepted Medium severity for a temporary withdrawal delay.

In the worst case (the `assert_eq!` panic path), the canister traps, which on the IC causes a full message rollback and prevents any state update until the next successful invocation.

---

### Likelihood Explanation

The minter uses 4 EVM RPC providers for mainnet with a `Threshold { total: Some(4), min: 3 }` consensus strategy at the `EvmRpcClient` level, but `NoReduction` is applied on top of the already-aggregated result. [7](#0-6) 

`eth_getTransactionReceipt` is particularly prone to inconsistent results across providers because different nodes may have different views of the chain at any given moment (e.g., one provider is slightly behind). Any such inconsistency causes `NoReduction` to return `Err(InconsistentResults)`, triggering the early `return`. This is a realistic, non-adversarial failure mode that occurs during normal network operation.

---

### Recommendation

1. **Per-item error handling**: Replace the `return` at line 442 with `continue`, so that a single failed receipt fetch does not abort processing of all other withdrawals. Log the failure and retry only the failed item on the next tick.
2. **Relax the reduction strategy**: Consider using a majority-based strategy (e.g., `StrictMajorityByKey`) for `get_transaction_receipt` instead of `NoReduction`, consistent with how `send_raw_transaction` uses `AnyOf`.
3. **Guard the assert**: Replace the `assert_eq!` at lines 447–450 with a graceful error log and early return, to prevent a canister trap in the unexpected-state scenario.

---

### Proof of Concept

1. User A queues a ckETH withdrawal → burn index 1, nonce 5.
2. User B queues a ckETH withdrawal → burn index 2, nonce 6.
3. Both nonces advance below the on-chain finalized transaction count.
4. `finalize_transactions_batch` is called by the timer.
5. `sent_transactions_to_finalize` returns hashes for both nonces (including any resubmissions).
6. `join_all` fires all `get_transaction_receipt` calls in parallel.
7. The receipt for one of nonce 5's transaction hashes returns `Err(InconsistentResults)` (two providers disagree on the receipt).
8. The loop hits line 442 and `return`s immediately.
9. The receipt for nonce 6 (User B), which was successfully fetched, is discarded.
10. Neither withdrawal is finalized. Both users wait for the next timer tick.
11. If the inconsistency persists across ticks (e.g., a provider is consistently behind), the DoS extends across multiple timer cycles. [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L363-383)
```rust
    for (signed_tx, result) in zip(transactions_to_send, results) {
        log!(DEBUG, "Sent transaction {signed_tx:?}: {result:?}");
        match result {
            Ok(SendRawTransactionStatus::Ok(_)) | Ok(SendRawTransactionStatus::NonceTooLow) => {
                // In case of resubmission we may hit the case of SendRawTransactionStatus::NonceTooLow
                // if the stuck transaction was mined in the meantime.
                // It will be cleaned-up once the transaction is finalized.
            }
            Ok(SendRawTransactionStatus::InsufficientFunds)
            | Ok(SendRawTransactionStatus::NonceTooHigh) => log!(
                INFO,
                "Failed to send transaction {signed_tx:?}: {result:?}. Will retry later.",
            ),
            Err(e) => {
                log!(
                    INFO,
                    "Failed to send transaction {signed_tx:?}: {e:?}. Will retry later."
                )
            }
        };
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L400-408)
```rust
            let results = join_all(txs_to_finalize.keys().map(async |hash| {
                rpc_client
                    .get_transaction_receipt(*hash)
                    .with_cycles(MIN_ATTACHED_CYCLES)
                    .try_send()
                    .await
                    .reduce_with_strategy(NoReduction)
            }))
            .await;
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L409-444)
```rust
            let mut receipts: BTreeMap<LedgerBurnIndex, EvmTransactionReceipt> = BTreeMap::new();
            for ((hash, withdrawal_id), result) in zip(txs_to_finalize, results) {
                match result {
                    Ok(Some(receipt)) => {
                        log!(
                            DEBUG,
                            "Received transaction receipt {receipt:?} for transaction {hash} and withdrawal ID {withdrawal_id}"
                        );
                        match receipts.get(&withdrawal_id) {
                            // by construction we never query twice the same transaction hash, which is a field in TransactionReceipt.
                            Some(existing_receipt) => {
                                log!(
                                    INFO,
                                    "ERROR: received different receipts for transaction {hash} with withdrawal ID {withdrawal_id}: {existing_receipt:?} and {receipt:?}. Will retry later"
                                );
                                return;
                            }
                            None => {
                                receipts.insert(withdrawal_id, receipt);
                            }
                        }
                    }
                    Ok(None) => {
                        log!(
                            DEBUG,
                            "Transaction {hash} for withdrawal ID {withdrawal_id} was not mined, it's probably a resubmitted transaction",
                        )
                    }
                    Err(e) => {
                        log!(
                            INFO,
                            "Failed to get transaction receipt for {hash} and withdrawal ID {withdrawal_id}: {e:?}. Will retry later",
                        );
                        return;
                    }
                }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L446-450)
```rust
            let actual_finalized_withdrawal_ids: BTreeSet<_> = receipts.keys().cloned().collect();
            assert_eq!(
                expected_finalized_withdrawal_ids, actual_finalized_withdrawal_ids,
                "ERROR: unexpected transaction receipts for some withdrawal IDs"
            );
```

**File:** rs/ethereum/cketh/minter/src/eth_rpc_client/mod.rs (L30-63)
```rust
pub fn rpc_client(state: &State) -> EvmRpcClient<IcRuntime, CandidResponseConverter, DoubleCycles> {
    const TOTAL_NUMBER_OF_PROVIDERS: u8 = 4;
    const MAX_NUM_RETRIES: u32 = 10;

    let chain = state.ethereum_network();
    let evm_rpc_id = state.evm_rpc_id();

    let providers = match chain {
        EthereumNetwork::Mainnet => EvmRpcServices::EthMainnet(None),
        EthereumNetwork::Sepolia => EvmRpcServices::EthSepolia(Some(vec![
            EthSepoliaService::BlockPi,
            EthSepoliaService::PublicNode,
            EthSepoliaService::Alchemy,
            EthSepoliaService::Ankr,
        ])),
    };

    let min_threshold = match chain {
        EthereumNetwork::Mainnet => 3_u8,
        EthereumNetwork::Sepolia => 2_u8,
    };
    assert!(
        min_threshold <= TOTAL_NUMBER_OF_PROVIDERS,
        "BUG: min_threshold too high"
    );

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

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L654-678)
```rust
    pub fn sent_transactions_to_finalize(
        &self,
        finalized_transaction_count: &TransactionCount,
    ) -> BTreeMap<Hash, LedgerBurnIndex> {
        let first_non_finalized_tx_nonce: TransactionNonce =
            finalized_transaction_count.change_units();
        let mut transactions = BTreeMap::new();
        for (_nonce, index, sent_txs) in self
            .sent_tx
            .iter()
            .filter(|(nonce, _burn_index, _signed_txs)| *nonce < &first_non_finalized_tx_nonce)
        {
            for sent_tx in sent_txs {
                if let Some(prev_index) = transactions.insert(sent_tx.as_ref().hash(), *index) {
                    assert_eq!(
                        prev_index,
                        *index,
                        "BUG: duplicate transaction hash {} for burn indices {prev_index} and {index}",
                        sent_tx.as_ref().hash()
                    );
                }
            }
        }
        transactions
    }
```
