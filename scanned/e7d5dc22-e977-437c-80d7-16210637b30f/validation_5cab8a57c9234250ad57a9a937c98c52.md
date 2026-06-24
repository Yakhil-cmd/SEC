### Title
ckBTC Minter `finalize_transaction` Panics on Zero `withdrawal_fee`, Permanently Halting BTC Withdrawals - (`File: rs/bitcoin/ckbtc/minter/src/state.rs`)

### Summary

The ckBTC minter's `finalize_transaction` function contains a hard `assert!(fee > 0, "withdraw_fee is zero")` guard inside the `ToCancel` branch. When a submitted transaction carries `withdrawal_fee: None` (or a zero-valued fee), the minter's heartbeat-driven finalization loop traps deterministically, leaving all affected ckBTC → BTC withdrawal requests permanently stuck. This is the exact same vulnerability class as the Perennial M-3 report: a caller computes a zero/missing amount due to a limit condition, still invokes a downstream function with that value, and the downstream invariant check causes an unrecoverable halt.

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/state.rs`, `finalize_transaction` handles three variants of `SubmittedWithdrawalRequests`. The `ToCancel` branch reads:

```rust
SubmittedWithdrawalRequests::ToCancel { requests, reason } => {
    let requests = requests.into_iter().collect::<BTreeSet<_>>();
    let fee = finalized_tx.withdrawal_fee.unwrap_or_default();
    let fee = fee.bitcoin_fee + fee.minter_fee;
    assert!(fee > 0, "withdraw_fee is zero");   // ← hard panic
``` [1](#0-0) 

`withdrawal_fee` is typed `Option<WithdrawalFee>`. When it is `None`, `unwrap_or_default()` yields `WithdrawalFee { bitcoin_fee: 0, minter_fee: 0 }`, so `fee = 0`, and the `assert!` fires. Because this code runs inside the minter's heartbeat timer (via `confirm_transaction` → `finalize_transaction`), the trap rolls back the heartbeat message but the stuck transaction remains in `submitted_transactions` or `stuck_transactions`. Every subsequent heartbeat re-enters the same path and traps again, permanently blocking all pending withdrawals.

The upstream trigger is `resubmit_transactions` in `rs/bitcoin/ckbtc/minter/src/lib.rs`. When a stuck transaction is being replaced with a cancellation transaction, the new `SubmittedBtcTransaction` is constructed with `withdrawal_fee: Some(total_fee)`:

```rust
let new_tx = state::SubmittedBtcTransaction {
    ...
    withdrawal_fee: Some(total_fee),
    ...
};
``` [2](#0-1) 

However, if the fee computation path produces `total_fee = WithdrawalFee { bitcoin_fee: 0, minter_fee: 0 }` (e.g., because the selected UTXOs exactly cover the output amount with no room for fees, or because an older transaction in state predates the `withdrawal_fee` field and carries `None`), the downstream `assert!` in `finalize_transaction` will panic. The upgrade proposal for the ckBTC minter explicitly confirms this scenario occurred on mainnet:

> "There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains why those transactions are currently stuck." [3](#0-2) 

### Impact Explanation

Any ckBTC → BTC withdrawal request that ends up in a `ToCancel` replacement transaction with a zero or missing `withdrawal_fee` causes the minter's heartbeat to trap on every invocation. Because the heartbeat is the sole mechanism for finalizing submitted transactions, **all** pending withdrawals (not just the affected one) are blocked for the duration of the stuck state. Funds are not lost but are inaccessible until a canister upgrade patches the assert. This matches the Perennial M-3 impact: no direct fund loss, but a critical operational process is permanently halted.

### Likelihood Explanation

The condition is reachable without any privileged access. Any user submitting a `retrieve_btc` request can trigger the chain of events:

1. User calls `retrieve_btc` with a valid amount.
2. The minter's fee estimator returns an anomalously low fee per vbyte (as happened on mainnet 2025-06-21).
3. The Bitcoin network does not mine the transaction.
4. The minter's heartbeat detects the stuck transaction and calls `resubmit_transactions`.
5. If the replacement transaction is a `ToCancel` variant and `withdrawal_fee` resolves to zero, `finalize_transaction` panics on every subsequent heartbeat.

The mainnet upgrade proposal confirms this sequence occurred with real user withdrawals.

### Recommendation

Replace the hard `assert!` with a graceful error path that logs the anomaly and skips or reimburses the affected request without trapping:

```rust
if fee == 0 {
    log!(Priority::Error, "withdraw_fee is zero for txid {txid}; skipping cancellation");
    // reimburse or mark as failed without panicking
    return None;
}
```

Additionally, the `resubmit_transactions` path should validate that `total_fee > 0` before constructing a `ToCancel` replacement transaction, mirroring the Perennial recommendation of "exit the function if the calculated amount is zero."

### Proof of Concept

1. Submit a `retrieve_btc` request when the minter's fee estimator is returning near-zero fees.
2. Observe the transaction is not mined (stuck in mempool or evicted).
3. Wait for `MIN_RESUBMISSION_DELAY`; the minter calls `resubmit_transactions`.
4. If the UTXO set produces `total_fee = 0` for the cancellation transaction, `finalize_transaction` is called with `withdrawal_fee: Some(WithdrawalFee { bitcoin_fee: 0, minter_fee: 0 })`.
5. `assert!(fee > 0, "withdraw_fee is zero")` fires; the heartbeat traps.
6. All subsequent heartbeats trap on the same path; no withdrawals can be finalized.

This is confirmed by the mainnet incident documented in: [4](#0-3) 

and the corresponding fix in PR #5713 referenced therein.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1049-1053)
```rust
            SubmittedWithdrawalRequests::ToCancel { requests, reason } => {
                let requests = requests.into_iter().collect::<BTreeSet<_>>();
                let fee = finalized_tx.withdrawal_fee.unwrap_or_default();
                let fee = fee.bitcoin_fee + fee.minter_fee;
                assert!(fee > 0, "withdraw_fee is zero");
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1003-1014)
```rust
                let new_tx = state::SubmittedBtcTransaction {
                    requests: new_tx_requests,
                    used_utxos: input_utxos,
                    txid: new_txid,
                    submitted_at: runtime.time(),
                    change_output: Some(change_output),
                    effective_fee_per_vbyte: Some(fee_rate),
                    withdrawal_fee: Some(total_fee),
                    // Do not fill signed_tx because this is not a consolidation transaction
                    signed_tx: None,
                };
                replace_transaction(old_txid, new_tx, replaced_reason);
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L17-33)
```markdown
## Motivation

Upgrade the ckBTC minter to try to unblock three transactions ckBTC → BTC (withdrawals) that are currently stuck since
2025.06.21.

After analysis, see this
forum [**post**](https://forum.dfinity.org/t/ckbtc-a-canister-issued-bitcoin-twin-token-on-the-ic-1-1-backed-by-btc/17606/202)
for more details, the problem appears to be due to the following:

1. An extremely low fee per vbyte was chosen by the minter for those transactions, which prevented them from being mined
   in the first place. We currently don’t have a satisfying explanation for how this low median fee was computed and are
   also investigating the bitcoin canister. A stop-gap solution was introduced
   in [#5742](https://github.com/dfinity/ic/pull/5742), to ensure that the fee per vbyte computed by the minter is
   always at least 1.5 sats/vbyte (for Bitcoin Mainnet).
2. There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains
   why those transactions are currently stuck. This should be completely fixed
   by [#5713](https://github.com/dfinity/ic/pull/5713).
```
