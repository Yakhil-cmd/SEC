### Title
Inconsistent `ReadOnly` mode enforcement in ckBTC minter — timer-based `reimburse_withdrawals` mints ckBTC without mode check — (File: rs/bitcoin/ckbtc/minter/src/tasks.rs)

---

### Summary

The ckBTC minter's `ReadOnly` mode is documented as preventing "any state modifications," but the timer-driven `ProcessLogic` task calls `reimburse_withdrawals` (which mints ckBTC) and `submit_pending_requests` (which signs and submits Bitcoin transactions) without checking the minter's mode. User-facing ingress paths (`update_balance`, `retrieve_btc`, `retrieve_btc_with_approval`) all gate on the mode, but the timer path does not, creating the same asymmetric guard enforcement described in the external report.

---

### Finding Description

The ckBTC minter defines a `Mode` enum in `rs/bitcoin/ckbtc/minter/src/state.rs`:

```rust
pub enum Mode {
    /// Minter's state is read-only.
    ReadOnly,
    RestrictedTo(Vec<Principal>),
    DepositsRestrictedTo(Vec<Principal>),
    #[default]
    GeneralAvailability,
}
``` [1](#0-0) 

The `ReadOnly` variant is documented as "Minter's state is read-only" / "The minter does not allow any state modifications." The DID interface reinforces this: `ReadOnly: // The minter does not allow any state modifications.` [2](#0-1) 

All three user-facing update endpoints correctly gate on the mode:

- `update_balance` calls `s.mode.is_deposit_available_for(&caller)` before proceeding: [3](#0-2) 

- `retrieve_btc` calls `s.mode.is_withdrawal_available_for(&caller)`: [4](#0-3) 

- `retrieve_btc_with_approval` calls `s.mode.is_withdrawal_available_for(&caller)`: [5](#0-4) 

However, the timer-driven `run_task` for `TaskType::ProcessLogic` contains **no mode check** before calling the three state-modifying functions:

```rust
TaskType::ProcessLogic => {
    // ...
    submit_pending_requests(&runtime).await;   // signs & submits BTC txs
    finalize_requests(&runtime).await;          // updates tx state
    reimburse_withdrawals(&runtime).await;      // MINTS ckBTC
}
``` [6](#0-5) 

`reimburse_withdrawals` mints ckBTC tokens back to users whose BTC withdrawals failed — a direct "credit" operation — without ever consulting `s.mode`. `submit_pending_requests` signs and broadcasts Bitcoin transactions — a state modification — equally without a mode check.

The `is_deposit_available_for` and `is_withdrawal_available_for` methods both return `Err` for `ReadOnly`: [7](#0-6) 

The existing integration test `test_upgrade_read_only` only verifies that `update_balance` and `retrieve_btc` are rejected; it does not verify that the timer stops processing: [8](#0-7) 

---

### Impact Explanation

When an operator sets the minter to `ReadOnly` mode to halt all operations during a security incident (e.g., a discovered minting bug), the global timer continues to fire every 5 seconds and execute `ProcessLogic`. This causes:

1. **ckBTC minting via `reimburse_withdrawals`**: Any withdrawal requests that were already queued and subsequently failed will have their ckBTC reimbursed — new tokens minted — despite the minter being in `ReadOnly` mode. This is a **chain-fusion mint bug**: the emergency mode does not prevent token issuance.
2. **BTC disbursement via `submit_pending_requests`**: Already-queued withdrawal requests continue to be signed and submitted to the Bitcoin network, moving funds even when the operator intended to freeze all activity.

The inconsistency directly mirrors the external report: the "credit" path (`reimburse_withdrawals`) lacks the guard that the "debit" path (`retrieve_btc`) has, allowing token credits to occur during an emergency pause.

---

### Likelihood Explanation

- The `ReadOnly` mode is the designated emergency stop for the ckBTC minter. It is set via a governance-controlled upgrade, meaning it is used in real operational scenarios.
- Any withdrawal request accepted before the mode change (which is common — the mode change takes effect at the next upgrade boundary) will have pending reimbursements that the timer will process.
- The timer fires automatically every 5 seconds with no user interaction required; no attacker action is needed beyond having submitted a withdrawal request before the mode change.
- The ckDOGE minter shares the same `ic_ckbtc_minter` library and timer logic, so it is equally affected. [9](#0-8) 

---

### Recommendation

Add a mode check at the top of the `ProcessLogic` task handler in `rs/bitcoin/ckbtc/minter/src/tasks.rs` before calling state-modifying functions:

```rust
TaskType::ProcessLogic => {
    // Do not process state-modifying tasks in ReadOnly mode.
    if crate::state::read_state(|s| s.mode == crate::state::Mode::ReadOnly) {
        return;
    }
    // ...
    submit_pending_requests(&runtime).await;
    finalize_requests(&runtime).await;
    reimburse_withdrawals(&runtime).await;
}
```

Alternatively, add the check inside `reimburse_withdrawals` itself (analogous to adding `whenNotPaused` to `_credit`), and similarly inside `submit_pending_requests` if full freeze semantics are desired. The existing `test_upgrade_read_only` test should be extended to assert that the timer does not mint ckBTC or submit BTC transactions when the minter is in `ReadOnly` mode.

---

### Proof of Concept

1. Deploy ckBTC minter in `GeneralAvailability` mode.
2. User calls `retrieve_btc` — request is accepted and queued (`Pending` state).
3. Operator upgrades minter with `mode: Some(Mode::ReadOnly)` to halt all operations.
4. Verify `update_balance` and `retrieve_btc` now return `TemporarilyUnavailable` (as tested in `test_upgrade_read_only`).
5. Advance the state machine by one tick (global timer fires, `ProcessLogic` runs).
6. Observe that `submit_pending_requests` still signs and submits the BTC transaction.
7. If the BTC transaction fails, observe that `reimburse_withdrawals` still mints ckBTC back to the user — a state modification — despite `ReadOnly` mode being active.

The root cause is at `rs/bitcoin/ckbtc/minter/src/tasks.rs` lines 134–151: `run_task` for `ProcessLogic` invokes `reimburse_withdrawals` and `submit_pending_requests` with no call to `read_state(|s| s.mode == Mode::ReadOnly)` or any equivalent guard. [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L343-353)
```rust
pub enum Mode {
    /// Minter's state is read-only.
    ReadOnly,
    /// Only the specified principals can modify the minter's state.
    RestrictedTo(Vec<Principal>),
    /// Only the specified principals can deposit BTC.
    DepositsRestrictedTo(Vec<Principal>),
    #[default]
    /// No restrictions on the minter interactions.
    GeneralAvailability,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L357-388)
```rust
    pub fn is_deposit_available_for(&self, p: &Principal) -> Result<(), String> {
        match self {
            Self::GeneralAvailability => Ok(()),
            Self::ReadOnly => Err("the minter is in read-only mode".to_string()),
            Self::RestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("access to the minter is temporarily restricted".to_string());
                }
                Ok(())
            }
            Self::DepositsRestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("BTC deposits are temporarily restricted".to_string());
                }
                Ok(())
            }
        }
    }

    /// Returns Ok if the specified principal can convert ckBTC to BTC.
    pub fn is_withdrawal_available_for(&self, p: &Principal) -> Result<(), String> {
        match self {
            Self::GeneralAvailability | Self::DepositsRestrictedTo(_) => Ok(()),
            Self::ReadOnly => Err("the minter is in read-only mode".to_string()),
            Self::RestrictedTo(allow_list) => {
                if !allow_list.contains(p) {
                    return Err("BTC withdrawals are temporarily restricted".to_string());
                }
                Ok(())
            }
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L182-191)
```text
type Mode = variant {
    // The minter does not allow any state modifications.
    ReadOnly;
    // Only specified principals can modify minter's state.
    RestrictedTo : vec principal;
    // Only specified principals can convert BTC to ckBTC.
    DepositsRestrictedTo : vec principal;
    // Anyone can interact with the minter.
    GeneralAvailability;
};
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L159-160)
```rust
    state::read_state(|s| s.mode.is_deposit_available_for(&caller))
        .map_err(UpdateBalanceError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L152-153)
```rust
    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L250-251)
```rust
    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcWithApprovalError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/tasks.rs (L134-151)
```rust
pub(crate) async fn run_task<R: CanisterRuntime>(task: Task, runtime: R) {
    match task.task_type {
        TaskType::ProcessLogic => {
            const INTERVAL_PROCESSING: Duration = Duration::from_secs(5);

            let _enqueue_followup_guard = guard((), |_| {
                schedule_after(INTERVAL_PROCESSING, TaskType::ProcessLogic, &runtime)
            });

            let _guard = match crate::guard::TimerLogicGuard::new() {
                Some(guard) => guard,
                None => return,
            };

            submit_pending_requests(&runtime).await;
            finalize_requests(&runtime).await;
            reimburse_withdrawals(&runtime).await;
        }
```

**File:** rs/bitcoin/ckbtc/minter/tests/tests.rs (L410-468)
```rust
#[test]
fn test_upgrade_read_only() {
    let env = new_state_machine();
    let ledger_id = install_ledger(&env);
    let minter_id = install_minter(&env, ledger_id);

    let authorized_principal =
        Principal::from_str("k2t6j-2nvnp-4zjm3-25dtz-6xhaa-c7boj-5gayf-oj3xs-i43lp-teztq-6ae")
            .unwrap();

    // upgrade
    let upgrade_args = UpgradeArgs {
        mode: Some(Mode::ReadOnly),
        ..Default::default()
    };
    let minter_arg = MinterArg::Upgrade(Some(upgrade_args));
    env.upgrade_canister(minter_id, minter_wasm(), Encode!(&minter_arg).unwrap())
        .expect("Failed to upgrade the minter canister");

    // when the mode is ReadOnly then the minter should reject all update calls.

    // 1. update_balance
    let update_balance_args = UpdateBalanceArgs {
        owner: None,
        subaccount: None,
    };
    let res = env
        .execute_ingress_as(
            authorized_principal.into(),
            minter_id,
            "update_balance",
            Encode!(&update_balance_args).unwrap(),
        )
        .expect("Failed to call update_balance");
    let res = Decode!(&res.bytes(), Result<Vec<UtxoStatus>, UpdateBalanceError>).unwrap();
    assert!(
        matches!(res, Err(UpdateBalanceError::TemporarilyUnavailable(_))),
        "unexpected result: {res:?}"
    );

    // 2. retrieve_btc
    let retrieve_btc_args = RetrieveBtcArgs {
        amount: 10,
        address: "".into(),
    };
    let res = env
        .execute_ingress_as(
            authorized_principal.into(),
            minter_id,
            "retrieve_btc",
            Encode!(&retrieve_btc_args).unwrap(),
        )
        .expect("Failed to call retrieve_btc");
    let res = Decode!(&res.bytes(), Result<RetrieveBtcOk, RetrieveBtcError>).unwrap();
    assert!(
        matches!(res, Err(RetrieveBtcError::TemporarilyUnavailable(_))),
        "unexpected result: {res:?}"
    );
}
```

**File:** rs/dogecoin/ckdoge/minter/src/main.rs (L46-61)
```rust
fn setup_tasks() {
    schedule_now(TaskType::ProcessLogic, &DOGECOIN_CANISTER_RUNTIME);
    schedule_now(TaskType::RefreshFeePercentiles, &DOGECOIN_CANISTER_RUNTIME);
}

#[unsafe(export_name = "canister_global_timer")]
fn timer() {
    // ic_ckbtc_minter::timer invokes ic_cdk::spawn
    // which must be wrapped in in_executor_context
    // as required by the new ic-cdk-executor.
    ic_cdk::futures::internals::in_executor_context(|| {
        #[cfg(feature = "self_check")]
        ok_or_die(check_invariants());

        ic_ckbtc_minter::timer(DOGECOIN_CANISTER_RUNTIME);
    });
```
