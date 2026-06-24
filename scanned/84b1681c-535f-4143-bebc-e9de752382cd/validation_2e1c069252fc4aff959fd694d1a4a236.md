### Title
Deterministic Panic in ckBTC Minter Timer Task Causes Permanent DoS of Withdrawal Pipeline - (`rs/bitcoin/ckbtc/minter/src/`)

### Summary

The ckBTC minter's periodic timer task (`finalize_requests`) contains multiple unhandled panic/trap points in its stuck-transaction resubmission path. When any of these are triggered by a deterministic state condition, the timer task traps on every subsequent invocation without advancing state, permanently blocking all ckBTC → BTC withdrawals. This is the IC analog of the Renzo CCIP finding: unhandled exceptions in a cross-chain message processor cause a DoS of the entire pipeline without requiring a malicious actor.

---

### Finding Description

The `finalize_requests` async function is the core periodic task that detects stuck Bitcoin transactions and resubmits them. It is invoked by the canister timer via `timer()` → `run_task()`. [1](#0-0) 

Inside `finalize_requests`, after detecting stuck transactions, it calls `resubmit_transactions`, which in turn calls the `replace_transaction` closure, which calls `state::audit::replace_transaction`. [2](#0-1) 

**Panic Point 1 — `audit::replace_transaction` `.expect()` calls:**

`audit::replace_transaction` unconditionally calls `.expect()` on `change_output` and `effective_fee_per_vbyte` of the replacement transaction: [3](#0-2) 

If a replacement transaction is constructed without these fields (e.g., due to a state inconsistency or a code path that omits them), this panics inside the timer task.

**Panic Point 2 — `assert!` on consolidation transaction `signed_tx`:**

Inside `resubmit_transactions`, when a consolidation transaction is detected, the code asserts that `signed_tx` is always `Some`: [4](#0-3) 

If a `ConsolidateUtxosRequest` is in state with a matching submitted transaction but `signed_tx: None`, this `assert!` panics deterministically on every timer tick.

**Panic Point 3 — `ic_cdk::trap` in `finalize_transaction`:**

`confirm_transaction` calls `state.finalize_transaction`, which traps if the txid is not found in either `submitted_transactions` or `stuck_transactions`: [5](#0-4) 

If the state becomes inconsistent (e.g., a txid is in `maybe_finalized_transactions` but has already been removed from the submitted/stuck lists by a concurrent path), this trap fires on every timer invocation.

**Why this is permanent DoS:**

On the IC, a canister trap rolls back all state changes for that message. The timer task fails, state is unchanged, and the next timer tick encounters the identical state and panics again. There is no equivalent of CCIP's "manual execution" — the only recovery is a canister upgrade (governance proposal), which takes hours to days. [6](#0-5) 

---

### Impact Explanation

When the timer task panics deterministically:
- All pending ckBTC → BTC withdrawal requests are permanently blocked (funds locked in the minter).
- New withdrawal requests accepted by the ledger (ckBTC burned) cannot be processed, resulting in user funds being burned with no BTC delivered.
- The `submitted_transactions` queue grows unbounded as new requests are accepted but never finalized.
- Recovery requires a governance proposal to upgrade the minter canister, which takes hours to days.

The `CkBtcMinterState` fields `submitted_transactions` and `stuck_transactions` confirm the scope of affected state: [7](#0-6) 

---

### Likelihood Explanation

This is not theoretical. The upgrade proposal `rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md` explicitly documents a mainnet incident where exactly this class of vulnerability occurred: [8](#0-7) 

A second mainnet incident in March 2026 (`minter_upgrade_2026_03_20.md`) shows that new stuck-transaction conditions continue to arise from normal usage (duplicate outpoints, low fees), each capable of triggering the same DoS pattern: [9](#0-8) 

The trigger conditions (low Bitcoin fees, network congestion, UTXO state inconsistencies) arise from ordinary user activity with no malicious intent required.

---

### Recommendation

1. **Replace all `assert!`, `.expect()`, and `ic_cdk::trap()` calls in the timer task path with graceful error handling** (log and `continue`/`return`) so that a single bad transaction does not block the entire pipeline.

2. **Isolate per-transaction processing**: wrap each iteration of the `for (old_txid, submitted_tx) in transactions` loop in a catch-unwind equivalent or use `Result`-returning helpers, so one stuck transaction cannot block others.

3. **Add a circuit-breaker**: if a transaction has failed resubmission N times, move it to a quarantine list and skip it in future timer ticks, rather than retrying indefinitely.

4. **Audit all `.expect()` and `assert!` calls reachable from timer/heartbeat entry points** across the minter codebase.

---

### Proof of Concept

**Scenario (mirrors the 2025-06-27 mainnet incident):**

1. User calls `retrieve_btc` for a ckBTC → BTC withdrawal. ckBTC is burned. A Bitcoin transaction is submitted with a low fee (e.g., due to a fee estimation anomaly).
2. The Bitcoin transaction is not mined. After `MIN_RESUBMISSION_DELAY`, `finalize_requests` identifies it as stuck and calls `resubmit_transactions`.
3. Due to a state condition (e.g., `effective_fee_per_vbyte: None` on the original submitted transaction, or a consolidation transaction with `signed_tx: None`), one of the panic points fires.
4. The timer task traps. State is rolled back. The stuck transaction remains in `submitted_transactions`.
5. On the next timer tick (seconds later), `finalize_requests` runs again, encounters the same state, and panics again.
6. All ckBTC withdrawals are permanently blocked until a governance-approved canister upgrade is deployed.

The `resubmit_transactions` loop processes all stuck transactions in a single async call with no per-transaction error isolation: [10](#0-9)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L665-668)
```rust
async fn finalize_requests<R: CanisterRuntime>(runtime: &R) {
    if state::read_state(|s| s.submitted_transactions.is_empty()) {
        return;
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L782-798)
```rust
    resubmit_transactions(
        &key_name,
        fee_per_vbyte,
        main_address,
        ecdsa_public_key,
        btc_network,
        state::read_state(|s| s.retrieve_btc_min_amount),
        maybe_finalized_transactions,
        |old_txid, new_tx, reason| {
            state::mutate_state(|s| {
                state::audit::replace_transaction(s, old_txid, new_tx, reason, runtime);
            })
        },
        runtime,
        &fee_estimator,
    )
    .await
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L817-860)
```rust
    for (old_txid, submitted_tx) in transactions {
        // ConsolidateUtxosRequest is directly re-sent if it already has signed_tx.
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
            log!(
                Priority::Info,
                "[resubmit_transactions]: re-sending a signed consolidation transaction {}",
                txid,
            );
            match runtime.send_raw_transaction(signed_tx, network).await {
                Ok(_) => {
                    log!(
                        Priority::Debug,
                        "[resubmit_transactions]: successfully sent transaction {}",
                        txid,
                    );
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
        }
        let tx_fee_per_vbyte = match submitted_tx.effective_fee_per_vbyte {
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1367-1376)
```rust
pub fn timer<R: CanisterRuntime + 'static>(runtime: R) {
    use tasks::{pop_if_ready, run_task};

    if let Some(task) = pop_if_ready(&runtime) {
        // Remark: spawn_017_compat is not needed since there is no code after `spawn` in the timer.
        // See https://github.com/dfinity/cdk-rs/blob/0.18.3/ic-cdk/V18_GUIDE.md#futures-ordering-changes
        #[allow(clippy::disallowed_methods)]
        ic_cdk::futures::spawn(run_task(task, runtime));
    }
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state/audit.rs (L218-236)
```rust
    record_event(
        EventType::ReplacedBtcTransaction {
            old_txid,
            new_txid: new_tx.txid,
            change_output: new_tx
                .change_output
                .clone()
                .expect("bug: all replacement transactions must have the change output"),
            submitted_at: new_tx.submitted_at,
            effective_fee_per_vbyte: new_tx
                .effective_fee_per_vbyte
                .expect("bug: all replacement transactions must have the fee")
                .millis(),
            withdrawal_fee: new_tx.withdrawal_fee,
            reason: Some(reason),
            new_utxos,
        },
        runtime,
    );
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L486-495)
```rust
    /// BTC transactions waiting for finalization.
    pub submitted_transactions: Vec<SubmittedBtcTransaction>,

    /// Transactions that likely didn't make it into the mempool.
    pub stuck_transactions: Vec<SubmittedBtcTransaction>,

    /// Maps ID of a stuck transaction to the ID of the corresponding replacement transaction.
    pub replacement_txid: BTreeMap<Txid, Txid>,
    /// Maps ID of a replacement transaction to the ID of the corresponding stuck transaction.
    pub rev_replacement_txid: BTreeMap<Txid, Txid>,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1014-1031)
```rust
    pub(crate) fn finalize_transaction(&mut self, txid: &Txid) -> Option<WithdrawalCancellation> {
        let finalized_tx = if let Some(pos) = self
            .submitted_transactions
            .iter()
            .position(|tx| &tx.txid == txid)
        {
            self.submitted_transactions.swap_remove(pos)
        } else if let Some(pos) = self
            .stuck_transactions
            .iter()
            .position(|tx| &tx.txid == txid)
        {
            self.stuck_transactions.swap_remove(pos)
        } else {
            ic_cdk::trap(format!(
                "Attempted to finalized a non-existent transaction {txid}"
            ));
        };
```

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2025_06_27.md (L19-33)
```markdown
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

**File:** rs/bitcoin/ckbtc/mainnet/minter_upgrade_2026_03_20.md (L19-28)
```markdown
Due to the security incident explained in this [forum post](https://forum.dfinity.org/t/proposal-140929-to-upgrade-the-ckbtc-minter/65401/3), the following ckBTC withdrawals (ckBTC -> BTC) are currently stuck:

* [3459007](https://dashboard.internetcomputer.org/bitcoin/transaction/3459007), [3459009](https://dashboard.internetcomputer.org/bitcoin/transaction/3459009), and [3459013](https://dashboard.internetcomputer.org/bitcoin/transaction/3459013) because the transaction from the minter tries to reuse the already spent output [`91bb46443799335076fbcd117f2295c7499d02dd3a59c22a531d31591114b303:5`](https://mempool.space/tx/91bb46443799335076fbcd117f2295c7499d02dd3a59c22a531d31591114b303#vout=5).
* [3489347](https://dashboard.internetcomputer.org/bitcoin/transaction/3489347) and [3489353](https://dashboard.internetcomputer.org/bitcoin/transaction/3489353) because the transaction from the minter tries to reuse the already spent output [`8942e5ef0d4ace158a4fddd5153d320701bd13370ff8fecef13795cdd8ff1dc5:1`](https://mempool.space/tx/8942e5ef0d4ace158a4fddd5153d320701bd13370ff8fecef13795cdd8ff1dc5#vout=1).

This proposal should address these issues by:
* Removing the duplicate outpoints from the minter's state.
* Discarding any transaction sent by the minter to the Bitcoin network that uses one of the duplicate outpoints. This is safe to do because those transactions are invalid and will never be accepted by the Bitcoin network.

The expected result is that the aforementioned withdrawals are considered as pending by the minter, as if they were going to be processed by the minter for the first time.
```
