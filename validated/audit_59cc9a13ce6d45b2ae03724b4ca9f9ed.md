Audit Report

## Title
Batch-Aborting `return` on Single `get_transaction_receipt` Error Temporarily Blocks All Pending ckETH/ckERC20 Withdrawal Finalizations — (File: `rs/ethereum/cketh/minter/src/withdraw.rs`)

## Summary
In `finalize_transactions_batch`, a single `Err` result from any `get_transaction_receipt` call causes an immediate `return` at line 442, discarding all successfully fetched receipts for other pending withdrawals in the same batch. Because `NoReduction` is the strictest aggregation strategy — returning `Err(InconsistentResults)` whenever any provider disagrees — this is a realistic, non-adversarial failure mode that temporarily prevents all pending ckETH/ckERC20 withdrawals from being finalized in the affected timer tick. A secondary `assert_eq!` at lines 447–450 can panic the canister if the finalized transaction count races ahead of receipt availability.

## Finding Description
`finalize_transactions_batch` (line 386) collects all transaction hashes for nonces below the on-chain finalized count via `sent_transactions_to_finalize`, fires all `get_transaction_receipt` calls in parallel with `join_all`, then iterates over results. The `Err` arm at line 437–443 calls `return`, exiting the entire async function:

```rust
Err(e) => {
    log!(INFO, "Failed to get transaction receipt for {hash} ...: {e:?}. Will retry later");
    return;   // ← aborts the entire batch
}
```

All receipts already collected for other withdrawal IDs in the same `join_all` batch are silently discarded; no `FinalizedTransaction` events are emitted for any withdrawal in that tick.

The `get_transaction_receipt` calls use `NoReduction` (line 406), which returns `Err(MultiCallError::InconsistentResults(...))` whenever the EVM RPC canister's aggregated result is not unanimous across providers. This is the strictest available strategy. By contrast, `send_transactions_batch` uses `AnyOf` (line 359) and handles errors per-item without aborting the loop.

The secondary risk: after the loop, `assert_eq!(expected_finalized_withdrawal_ids, actual_finalized_withdrawal_ids, ...)` at lines 447–450 panics if any nonce below the finalized count produced only `Ok(None)` receipts (all resubmitted hashes unrecognized by providers). A canister panic in a timer callback causes a full message rollback on the IC, preventing any state update until the next successful invocation. If the condition persists across ticks, the DoS extends.

## Impact Explanation
Every ckETH and ckERC20 withdrawal in the `sent_tx` state whose nonce is below the on-chain finalized count is blocked from completing in the affected timer tick. Users experience delayed withdrawals. In the `assert_eq!` panic scenario, the canister traps on every tick for as long as the triggering condition persists, extending the DoS beyond a single tick. This constitutes a concrete, demonstrable availability impact on the ckETH/ckERC20 Chain Fusion financial integration — matching the allowed Medium impact: *"moderate user-funds/security impact"* and *"Significant Chain Fusion, ck-token … security impact with concrete user or protocol harm."*

## Likelihood Explanation
The minter uses 4 EVM RPC providers on mainnet with a `Threshold { total: Some(4), min: 3 }` consensus strategy, but `NoReduction` is applied on top of the already-aggregated result. `eth_getTransactionReceipt` is particularly prone to provider disagreement because different nodes may have different chain views at any moment (e.g., one provider is slightly behind). Any such disagreement causes `NoReduction` to return `Err(InconsistentResults)`, triggering the early `return`. This is a realistic, non-adversarial failure mode that occurs during normal Ethereum network operation and requires no attacker action. The `assert_eq!` panic path requires the finalized count to race ahead of receipt availability across providers, which is also a realistic transient condition.

## Recommendation
1. **Per-item error handling**: Replace the `return` at line 442 with `continue`, so a single failed receipt fetch does not abort processing of all other withdrawals. Log the failure and retry only the failed item on the next tick.
2. **Relax the reduction strategy**: Consider using `StrictMajorityByKey` for `get_transaction_receipt` instead of `NoReduction`, consistent with how other calls use majority-based strategies.
3. **Guard the assert**: Replace the `assert_eq!` at lines 447–450 with a graceful error log and early return to prevent a canister trap in the unexpected-state scenario.

## Proof of Concept
1. Two ckETH withdrawals are queued: burn index 1 (nonce 5), burn index 2 (nonce 6). Both nonces advance below the on-chain finalized transaction count.
2. `finalize_transactions_batch` is invoked by the timer.
3. `sent_transactions_to_finalize` returns hashes for both nonces (including any resubmissions).
4. `join_all` fires all `get_transaction_receipt` calls in parallel.
5. One provider returns a slightly different receipt for nonce 5's hash (e.g., it is one block behind); `NoReduction` returns `Err(InconsistentResults)`.
6. The loop hits line 442 and `return`s immediately.
7. The receipt for nonce 6 (successfully fetched) is discarded.
8. Neither withdrawal is finalized. Both users wait for the next timer tick.
9. If the provider inconsistency persists, the DoS extends across multiple timer cycles.

A deterministic integration test can reproduce this by mocking the EVM RPC canister to return inconsistent results for one hash while returning a valid receipt for another, then asserting that `FinalizedTransaction` events are emitted for the unaffected withdrawal — which the current code fails to do.