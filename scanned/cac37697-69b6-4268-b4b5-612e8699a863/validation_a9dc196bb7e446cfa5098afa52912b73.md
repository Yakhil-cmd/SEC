### Title
Non-Atomic Upgrade Leaves ckERC20 Ledger/Index Canister Permanently Stopped on `upgrade_canister` Failure - (File: rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs)

### Summary
The `upgrade_canister` function in the Ledger Suite Orchestrator performs three sequential inter-canister calls — `stop_canister`, `upgrade_canister` (install_code), and `start_canister` — without atomicity. If `upgrade_canister` succeeds but `start_canister` fails (or if `upgrade_canister` itself fails after `stop_canister` succeeds), the managed ckERC20 ledger or index canister is left in a **stopped** state. Unlike the SNS governance's `upgrade_canister_directly`, which unconditionally attempts to restart the canister even after a failed install, the orchestrator returns early via `?` without issuing a compensating `start_canister`. The canister remains stopped until the next timer retry, blocking all ckERC20 token transfers and index synchronization during that window.

### Finding Description

The `upgrade_canister` function at `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs` lines 1271–1314 executes three sequential awaited inter-canister calls:

```
stop_canister  →  upgrade_canister  →  start_canister  →  record_upgrade_completed
```

Each step uses the `?` operator to propagate errors: [1](#0-0) 

If `stop_canister` succeeds and `upgrade_canister` then fails (e.g., transient IC error, cycles exhaustion, or a wasm that traps during `canister_post_upgrade`), the function returns `Err(UpgradeLedgerSuiteError::UpgradeCanisterError)` immediately — **without issuing `start_canister`**. The managed ledger or index canister is now stopped with its old wasm, and the orchestrator's internal `completed_upgrades` map is not updated.

Similarly, if `upgrade_canister` succeeds but `start_canister` fails, the canister is upgraded to the new wasm but remains stopped, and `record_upgrade_completed` is never called, leaving the orchestrator's state inconsistent with on-chain reality. [2](#0-1) 

Compare this with the SNS governance's `upgrade_canister_directly`, which deliberately avoids the `?` operator after `install_code` so that `start_canister` is always attempted regardless of upgrade outcome: [3](#0-2) 

The orchestrator's `UpgradeLedgerSuiteError::UpgradeCanisterError` and `StartCanisterError` are both marked `is_recoverable() = true`: [4](#0-3) 

So the task is retried on the next timer tick. However, the retry re-executes the full sequence starting from `stop_canister` — it does **not** issue a standalone `start_canister` to recover the stopped canister first. During the entire retry window (which can span multiple timer periods if the error is persistent), the ckERC20 ledger or index canister remains stopped.

The `ManagedCanisterStatus` in the orchestrator's state is also never updated to reflect the new wasm hash after an upgrade — only `completed_upgrades` is updated via `record_upgrade_completed`: [5](#0-4) 

This means the orchestrator's view of the installed wasm hash diverges from on-chain reality whenever `start_canister` fails after a successful `upgrade_canister`.

### Impact Explanation

**Impact: High.**

The ckERC20 ledger canister being stopped means:
- All ICRC-1 token transfers (`icrc1_transfer`) are rejected.
- The ckERC20 index canister cannot sync new blocks from the ledger.
- Chain-fusion minting and burning flows that depend on the ledger are blocked.
- Users holding ckERC20 tokens (e.g., ckUSDC, ckUSDT) cannot move funds during the outage window.

If the new wasm's `canister_post_upgrade` or `canister_start` hook traps, every retry will fail at `upgrade_canister` or `start_canister` respectively, and the canister remains stopped indefinitely until a governance intervention.

### Likelihood Explanation

**Likelihood: Low.**

The failure scenario requires either a transient IC-level error (e.g., subnet under load, cycles exhaustion of the orchestrator during the call) or a wasm whose upgrade hooks trap. Transient errors are realistic in production; a trapping upgrade hook is less likely given the wasm validation process but not impossible. The upgrade is triggered by an NNS governance proposal, so the window is bounded to post-proposal execution.

### Recommendation

Apply the same pattern used in `rs/sns/governance/src/canister_control.rs` (`upgrade_canister_directly`): remove the `?` after `upgrade_canister` and unconditionally attempt `start_canister` regardless of whether the upgrade succeeded or failed. This ensures the managed canister is never left in a stopped state due to a partial failure:

```rust
let upgrade_result = runtime
    .upgrade_canister(canister_id, wasm.to_bytes())
    .await
    .map_err(UpgradeLedgerSuiteError::UpgradeCanisterError);
// Always attempt to restart, even if upgrade failed.
runtime
    .start_canister(canister_id)
    .await
    .map_err(UpgradeLedgerSuiteError::StartCanisterError)?;
upgrade_result?;
```

Additionally, consider updating `ManagedCanisterStatus.installed_wasm_hash` after a successful upgrade so the orchestrator's internal state stays consistent with on-chain reality.

### Proof of Concept

1. An NNS governance proposal is executed that triggers `post_upgrade` on the orchestrator with a new `ledger_compressed_wasm_hash`.
2. The orchestrator schedules `Task::UpgradeLedgerSuite` for each managed ERC-20 token.
3. The timer fires and calls `upgrade_canister::<Ledger, _>(ledger_canister_id, &wasm_hash, runtime)`.
4. `stop_canister(ledger_canister_id)` succeeds — the ckUSDC ledger is now stopped.
5. `upgrade_canister(ledger_canister_id, wasm_bytes)` fails (e.g., transient `TransientInternalError` or the new wasm's `canister_post_upgrade` traps).
6. The function returns `Err(UpgradeLedgerSuiteError::UpgradeCanisterError(...))` immediately — `start_canister` is never called.
7. The ckUSDC ledger canister remains stopped. All `icrc1_transfer` calls to it are rejected with "canister is stopped".
8. `UpgradeLedgerSuiteError::UpgradeCanisterError` is `is_recoverable() = true`, so the task is rescheduled.
9. On the next timer tick, the sequence restarts from `stop_canister` (no-op on already-stopped canister), then retries `upgrade_canister`. If the error is persistent, the ledger remains stopped across all retries. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L231-249)
```rust
impl UpgradeLedgerSuite {
    /// Create a new upgrade ledger suite task containing multiple subtasks
    /// depending on which canisters need to be upgraded. Due to the dependencies between the canisters of a ledger suite, e.g.,
    /// the index pulls transactions from the ledger, the order of the subtasks is important.
    ///
    /// The order of the subtasks is as follows:
    /// 1. Upgrade the index canister
    /// 2. Upgrade the ledger canister
    /// 3. Fetch the list of archives from the ledger and upgrade all archive canisters
    ///
    /// For each canister, upgrading involves 3 (potentially failing) steps:
    /// 1. Stop the canister
    /// 2. Upgrade the canister
    /// 3. Start the canister
    ///
    /// Note that after having upgraded the index, but before having upgraded the ledger, the upgraded index may fetch information from the not yet upgraded ledger.
    /// However, this is deemed preferable to trying to do some kind of atomic upgrade,
    /// where the ledger would be stopped before upgrading the index, since this would result in 2 canisters being stopped at the same time,
    /// which could be more problematic, especially if for some unexpected reason the upgrade fails.
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L667-679)
```rust
impl UpgradeLedgerSuiteError {
    fn is_recoverable(&self) -> bool {
        match self {
            UpgradeLedgerSuiteError::TokenNotFound(_) => false,
            UpgradeLedgerSuiteError::CanisterNotReady { .. } => true,
            UpgradeLedgerSuiteError::WasmHashNotFound(_) => false,
            UpgradeLedgerSuiteError::WasmStoreError(_) => false,
            UpgradeLedgerSuiteError::StopCanisterError(_) => true,
            UpgradeLedgerSuiteError::StartCanisterError(_) => true,
            UpgradeLedgerSuiteError::UpgradeCanisterError(_) => true,
            UpgradeLedgerSuiteError::DiscoverArchivesError(e) => e.is_recoverable(),
        }
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1271-1314)
```rust
async fn upgrade_canister<T: StorableWasm, R: CanisterRuntime>(
    canister_id: Principal,
    wasm_hash: &WasmHash,
    runtime: &R,
) -> Result<(), UpgradeLedgerSuiteError> {
    let wasm = match read_wasm_store(|s| wasm_store_try_get::<T>(s, wasm_hash)) {
        Ok(Some(wasm)) => Ok(wasm),
        Ok(None) => Err(UpgradeLedgerSuiteError::WasmHashNotFound(wasm_hash.clone())),
        Err(e) => Err(UpgradeLedgerSuiteError::WasmStoreError(e)),
    }?;

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

    log!(
        DEBUG,
        "Upgrade of canister {} to {} completed",
        canister_id,
        wasm_hash
    );
    let now = runtime.time();
    mutate_state(|s| s.record_upgrade_completed(canister_id, wasm_hash.clone(), now));
    Ok(())
}
```

**File:** rs/sns/governance/src/canister_control.rs (L55-81)
```rust
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
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L667-680)
```rust
    pub fn record_upgrade_completed(
        &mut self,
        canister_id: Principal,
        wasm_hash: WasmHash,
        timestamp: u64,
    ) {
        self.completed_upgrades.insert(
            canister_id,
            CanisterUpgrade {
                wasm_hash,
                timestamp,
            },
        );
    }
```
