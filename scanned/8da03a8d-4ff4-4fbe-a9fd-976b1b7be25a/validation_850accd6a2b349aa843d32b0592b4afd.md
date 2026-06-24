### Title
Unconditional `start_canister` After SNS Root Upgrade Overwrites Pre-existing Stopped State - (File: `rs/sns/governance/src/canister_control.rs`)

---

### Summary

`upgrade_canister_directly` in `rs/sns/governance/src/canister_control.rs` unconditionally calls `start_canister` after performing a canister upgrade, without first recording whether the target canister was already `Stopped` before the operation began. This is the direct IC analog of the LPMigration bug: just as `liquidate()` unconditionally called `pause()` regardless of the contract's initial pause state, `upgrade_canister_directly` unconditionally calls `start_canister` regardless of the canister's initial run state.

---

### Finding Description

`upgrade_canister_directly` always executes three steps in sequence with no state-preservation logic:

1. `stop_canister(env, canister_id).await?` — line 46
2. `install_code(...)` — line 55
3. `start_canister(env, canister_id).await?` — line 73 [1](#0-0) 

No canister status is queried before step 1. If the target canister is already `Stopped`, `stop_canister` is a no-op (the management canister accepts it silently), the upgrade proceeds, and then `start_canister` is called unconditionally — transitioning the canister from `Stopped` to `Running` without any intent from the caller.

This function is invoked for the SNS root canister upgrade path in two places in `rs/sns/governance/src/governance.rs`:

- `perform_upgrade_to_next_sns_version_legacy` (line 2872), triggered by `UpgradeSnsToNextVersion` proposals
- `upgrade_sns_framework_canister` (line 2930), triggered by `AdvanceSnsTargetVersion` proposals [2](#0-1) [3](#0-2) 

The same structural flaw exists in `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs` `upgrade_canister` (lines 1282–1303), which also unconditionally stops then starts ckETH/ckERC20 ledger canisters during scheduler-driven upgrades without recording initial state. [4](#0-3) 

---

### Impact Explanation

An SNS DAO may deliberately stop its root canister — for example, after discovering a critical bug — by passing a governance proposal. If a concurrent or subsequent upgrade proposal (e.g., `UpgradeSnsToNextVersion`) is also approved and executed, `upgrade_canister_directly` will unconditionally restart the root canister after installing the new WASM. The root canister transitions from `Stopped` to `Running` without any explicit governance intent to restart it. A running root canister accepts and processes inter-canister calls; a canister that was stopped for security reasons is now exposed to message traffic the DAO intended to block.

For the ledger-suite-orchestrator path, a ckETH or ckERC20 ledger canister that was stopped (e.g., by the orchestrator itself during a prior failed operation) will be unconditionally restarted by the scheduler's upgrade routine, potentially resuming ledger operations before the DAO has verified the canister is safe to run.

---

### Likelihood Explanation

The SNS governance system allows any token holder with sufficient voting power to submit both `StopOrStartCanister` and `UpgradeSnsToNextVersion` proposals. These two proposal types are independent and can be approved in sequence. The scenario where a root canister is stopped for safety and an upgrade proposal is simultaneously in flight is realistic during incident response. The ledger-suite-orchestrator path is triggered automatically by a timer-driven scheduler, making it reachable without any governance vote once an upgrade task is queued.

---

### Recommendation

Before calling `stop_canister`, query the canister's current status and store it in a local variable. Use that saved state as a condition to decide whether to call `start_canister` at the end of the function. Concretely, in `upgrade_canister_directly`:

```rust
// Before stopping, record initial state
let initial_status = canister_status(env, canister_id).await?.status;

stop_canister(env, canister_id).await?;
// ... install code ...

// Only restart if the canister was originally running
if initial_status != CanisterStatusType::Stopped {
    start_canister(env, canister_id).await?;
}
```

Apply the same pattern to `upgrade_canister` in `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`. [5](#0-4) 

---

### Proof of Concept

1. An SNS DAO discovers a bug in its root canister and passes a `StopOrStartCanister` proposal with `action = Stop`. The root canister enters `Stopped` state.
2. Simultaneously (or shortly after), an `UpgradeSnsToNextVersion` proposal targeting the root canister is approved and executed.
3. SNS governance calls `perform_upgrade_to_next_sns_version_legacy`, which calls `upgrade_canister_directly(env, root_canister_id, ...)`.
4. Inside `upgrade_canister_directly`, `stop_canister` is called — the management canister accepts this silently since the canister is already stopped.
5. `install_code` installs the new WASM.
6. `start_canister` is called unconditionally at line 73.
7. The root canister is now `Running`, accepting inter-canister messages — despite the DAO's explicit intent to keep it stopped. [5](#0-4) [2](#0-1)

### Citations

**File:** rs/sns/governance/src/canister_control.rs (L34-82)
```rust
pub async fn upgrade_canister_directly(
    env: &dyn Environment,
    canister_id: CanisterId,
    wasm: Vec<u8>,
    arg: Vec<u8>,
) -> Result<(), GovernanceError> {
    log!(
        INFO,
        "{}Begin: Stop canister {}.",
        log_prefix(),
        canister_id
    );
    stop_canister(env, canister_id).await?;
    log!(INFO, "{}End: Stop canister {}.", log_prefix(), canister_id);

    log!(
        INFO,
        "{}Begin: Install code into canister {}",
        log_prefix(),
        canister_id
    );
    let install_result = install_code(env, canister_id, wasm, arg)
        // No question mark operator here, because we always want to re-start
        // the canister after attempting install_code, even if install_code
        // fails.
        .await;
    log!(
        INFO,
        "{}End: Install code into canister {}",
        log_prefix(),
        canister_id
    );

    log!(
        INFO,
        "{}Begin: Re-start canister {}",
        log_prefix(),
        canister_id
    );
    start_canister(env, canister_id).await?;
    log!(
        INFO,
        "{}End: Re-start canister {}",
        log_prefix(),
        canister_id
    );

    install_result
}
```

**File:** rs/sns/governance/src/governance.rs (L2869-2878)
```rust
        let target_is_root = canister_ids_to_upgrade.contains(&root_canister_id);

        if target_is_root {
            upgrade_canister_directly(
                &*self.env,
                root_canister_id,
                target_wasm,
                Encode!().unwrap(),
            )
            .await?;
```

**File:** rs/sns/governance/src/governance.rs (L2927-2936)
```rust
        let target_is_root = canister_type_to_upgrade == SnsCanisterType::Root;

        if target_is_root {
            upgrade_canister_directly(
                &*self.env,
                root_canister_id,
                target_wasm,
                Encode!().unwrap(),
            )
            .await?;
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1282-1303)
```rust
    log!(DEBUG, "Stopping canister {}", canister_id);
    runtime
        .stop_canister(canister_id)
        .await
        .map_err(UpgradeLedgerSuiteError::StopCanisterError)?;

    log!(
        DEBUG,
        "Upgrading wasm module of canister {} to {}",
        canister_id,
        wasm_hash
    );
    runtime
        .upgrade_canister(canister_id, wasm.to_bytes())
        .await
        .map_err(UpgradeLedgerSuiteError::UpgradeCanisterError)?;

    log!(DEBUG, "Starting canister {}", canister_id);
    runtime
        .start_canister(canister_id)
        .await
        .map_err(UpgradeLedgerSuiteError::StartCanisterError)?;
```
