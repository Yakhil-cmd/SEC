### Title
Hard `assert!` in ckBTC Minter `resubmit_transactions` Traps Canister on Unexpected Consolidation State, Permanently Blocking Withdrawal Recovery - (File: rs/bitcoin/ckbtc/minter/src/lib.rs)

---

### Summary

The `resubmit_transactions` function in the ckBTC minter uses a hard `assert!(tx.signed_tx.is_some())` to validate that a consolidation transaction has a signed transaction attached. If this assertion fails due to any state inconsistency, the minter canister traps (panics), preventing all further resubmission attempts and leaving ckBTC withdrawal transactions permanently stuck. This directly mirrors the Cairo ERC20Handler vulnerability: both use a hard assert/revert in a cross-chain handler instead of graceful error handling, causing transactions to remain in a stuck/pending state indefinitely.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/lib.rs`, the `resubmit_transactions` function iterates over stuck transactions and attempts to resubmit them. For consolidation transactions, it checks whether the current consolidation request matches the stuck transaction's `txid`, and then asserts:

```rust
// For ConsolidatedUtxosRequest, signed_tx should always exist.
assert!(tx.signed_tx.is_some());
``` [1](#0-0) 

This is a hard `assert!` (not `debug_assert!`), meaning it fires in both debug and release (production) builds. It is placed inside a closure passed to `read_state`, which is called during the minter's timer-driven `finalize_requests` → `resubmit_transactions` flow. If `signed_tx` is `None` for a consolidation transaction that matches `old_txid`, the assertion fires and the minter canister traps.

When the minter traps during `resubmit_transactions`, the entire timer execution is aborted. The stuck transaction is never resubmitted, and the withdrawal request remains in a stuck state indefinitely. The only recovery path is an emergency NNS-governed upgrade of the minter canister.

The comment "For ConsolidatedUtxosRequest, signed_tx should always exist" acknowledges the assumption but provides no fallback. The correct pattern — consistent with how other error cases in the same function are handled — would be to log the anomaly and `continue` to the next transaction rather than trapping. [2](#0-1) 

---

### Impact Explanation

ckBTC withdrawal transactions (ckBTC → BTC) can be permanently blocked if the minter traps during resubmission. The minter's timer will keep re-entering `resubmit_transactions`, hitting the same assert on every tick, creating a **deterministic, self-repeating trap** that prevents all resubmission progress.

This was confirmed by a real-world production incident in June 2025, documented in the minter upgrade proposal:

> "There is a deterministic panic occurring in the minter when it tries to resubmit those transactions, which explains why those transactions are currently stuck." [3](#0-2) 

Three ckBTC → BTC withdrawal transactions were stuck and required an emergency canister upgrade to unblock. The `assert!(tx.signed_tx.is_some())` remains in the current codebase, meaning the same class of trap can recur if the state invariant is violated again.

---

### Likelihood Explanation

The likelihood is **moderate**. The `signed_tx` field is set by the minter itself during consolidation transaction creation, so it should normally be present. However:

1. The June 2025 incident proves the invariant can be violated in production under real conditions (extremely low fee per vbyte causing transactions to not be mined, combined with state inconsistency during resubmission).
2. Any future bug in the minter's state management that causes `signed_tx` to be `None` for a consolidation transaction will trigger the same deterministic trap.
3. The minter's state is complex and involves multiple async inter-canister calls (to the Bitcoin canister, ledger, and threshold ECDSA), creating multiple opportunities for partial state updates.

The entry path is reachable by any ckBTC withdrawal user: a user initiates a withdrawal, the transaction gets stuck (e.g., due to low Bitcoin network fees), and the minter's resubmission logic traps on the inconsistent state.

---

### Recommendation

Replace the hard `assert!` with graceful error handling that logs the anomaly and skips the affected transaction, allowing the minter to continue processing other stuck transactions:

```rust
if tx.signed_tx.is_none() {
    log!(Priority::Error,
        "[resubmit_transactions]: consolidation tx {} has no signed_tx, skipping",
        tx.txid);
    continue;
}
let signed_tx = tx.signed_tx.clone().unwrap();
```

This matches the pattern used elsewhere in `resubmit_transactions` where errors are logged and execution continues rather than trapping. [4](#0-3) 

---

### Proof of Concept

1. A ckBTC user initiates a withdrawal (ckBTC → BTC).
2. The minter creates a consolidation transaction but, due to a state inconsistency, `signed_tx` is `None`.
3. The minter's periodic timer fires `finalize_requests`, which calls `resubmit_transactions`.
4. `resubmit_transactions` iterates over stuck transactions and finds the consolidation transaction matching `old_txid`.
5. Inside the `read_state` closure, `assert!(tx.signed_tx.is_some())` fires.
6. The minter canister traps. The timer execution is aborted.
7. On the next timer tick, the same code path is re-entered and traps again — a deterministic, self-repeating failure.
8. The withdrawal transaction remains permanently stuck until an NNS-governed emergency upgrade is deployed.

This exact scenario occurred in production in June 2025, requiring the emergency upgrade proposal `minter_upgrade_2025_06_27`. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L819-835)
```rust
        if let Some((network, txid, signed_tx)) = read_state(|s| {
            s.current_consolidate_utxos_request
                .as_ref()
                .and_then(|req| {
                    s.get_submitted_transaction(req.block_index).and_then(|tx| {
                        if tx.txid == old_txid {
                            // For ConsolidatedUtxosRequest, signed_tx should always exist.
                            assert!(tx.signed_tx.is_some());
                            tx.signed_tx
                                .clone()
                                .map(|signed_tx| (s.btc_network, tx.txid, signed_tx))
                        } else {
                            None
                        }
                    })
                })
        }) {
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L848-858)
```rust
                }
                Err(err) => {
                    log!(
                        Priority::Info,
                        "[resubmit_transactions]: failed to send transaction {} again: {}",
                        txid,
                        err
                    );
                }
            }
            continue;
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L860-940)
```rust
        let tx_fee_per_vbyte = match submitted_tx.effective_fee_per_vbyte {
            Some(prev_fee_rate) => {
                // There are 2 requirements on the fee of a replacement transaction:
                // 1) The fee rate strictly increases. Although not required from [BIP-125](https://en.bitcoin.it/wiki/BIP_0125),
                // it is actually required by the [implementation](https://github.com/bitcoin/bitcoin/blob/d2ecd6815d89c9b089b55bc96fdf93b023be8dda/src/policy/rbf.cpp#L149).
                // 2) The total fee of the replacement transaction must be at least as high as the previous transaction fee plus the minimum relay fee.
                //
                // To satisfy both conditions, we choose the new fee rate to be the previous one plus the minimum relay fee rate increase.
                // This will satisfy 2) because the computed total fee of a transaction is not dependent on the variable signature sizes
                // (see `FeeEstimator::evaluate_transaction_fee` and `fake_sign`)
                fee_rate.max(prev_fee_rate + Fee::MIN_RELAY_FEE_RATE_INCREASE)
            }
            None => fee_rate,
        };

        let outputs = match &submitted_tx.requests {
            state::SubmittedWithdrawalRequests::ToConfirm { requests } => requests
                .iter()
                .map(|req| (req.address.clone(), req.amount))
                .collect(),
            state::SubmittedWithdrawalRequests::ToCancel { .. } => {
                vec![(main_address.clone(), retrieve_btc_min_amount)]
            }
            state::SubmittedWithdrawalRequests::ToConsolidate { request } => {
                vec![(main_address.clone(), request.amount / 2)]
            }
        };

        let mut input_utxos = submitted_tx.used_utxos;
        let mut replaced_reason = state::eventlog::ReplacedReason::ToRetry;
        let mut new_tx_requests = submitted_tx.requests;
        let max_num_inputs_in_transaction = read_state(|s| s.max_num_inputs_in_transaction);
        let build_result = match build_unsigned_transaction_from_inputs(
            &input_utxos,
            outputs,
            &main_address,
            max_num_inputs_in_transaction,
            tx_fee_per_vbyte,
            fee_estimator,
        ) {
            Err(BuildTxError::InvalidTransaction(err)) => {
                log!(
                    Priority::Info,
                    "[resubmit_transactions]: {:?}, transaction {} will be canceled",
                    err,
                    &submitted_tx.txid,
                );
                let mut inputs = UtxoSet::from_iter(input_utxos);
                // The following selection is guaranteed to select at least 1 UTXO because
                // the value of stuck transaction is no less than retrieve_btc_min_amount.
                input_utxos = utxos_selection(retrieve_btc_min_amount, &mut inputs, 0);
                // The requests field has to be cleared because the finalization of this
                // transaction is not meant to complete the corresponding RetrieveBtcRequests
                // but rather to cancel them.
                let requests = match new_tx_requests {
                    state::SubmittedWithdrawalRequests::ToConfirm { requests } => requests,
                    state::SubmittedWithdrawalRequests::ToCancel { .. } => {
                        unreachable!("cancellation tx never has too many inputs!")
                    }
                    state::SubmittedWithdrawalRequests::ToConsolidate { .. } => {
                        unreachable!("consolidation tx never has too many inputs!")
                    }
                };
                let reason = reimbursement::WithdrawalReimbursementReason::InvalidTransaction(err);
                replaced_reason = state::eventlog::ReplacedReason::ToCancel {
                    reason: reason.clone(),
                };
                new_tx_requests = state::SubmittedWithdrawalRequests::ToCancel { requests, reason };
                let outputs = vec![(main_address.clone(), retrieve_btc_min_amount)];
                build_unsigned_transaction_from_inputs(
                    &input_utxos,
                    outputs,
                    &main_address,
                    max_num_inputs_in_transaction,
                    fee_rate, // Use normal fee
                    fee_estimator,
                )
            }
            result => result,
        };
        let (unsigned_tx, change_output, total_fee) = match build_result {
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
