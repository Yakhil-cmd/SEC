### Title
Single Failing Archive Canister Blocks All Remaining Archive Upgrades - (File: rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs)

### Summary
In `UpgradeLedgerSuiteSubtask::UpgradeArchives`, a sequential loop iterates through all archive canisters and propagates the first error immediately via `?`, leaving every archive that appears later in the list permanently unupgraded. The retry mechanism restarts the subtask from the beginning without skipping the broken archive, creating an indefinite blockage for all subsequent archives.

### Finding Description
The `UpgradeArchives` subtask in `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs` iterates through all archive canisters sequentially:

```rust
//We expect usually 0 or 1 archive, so a simple sequential strategy is good enough.
for canister_id in archives {
    upgrade_canister::<Archive, _>(canister_id, compressed_wasm_hash, runtime)
        .await?;
}
``` [1](#0-0) 

The `upgrade_canister` function performs three sequential inter-canister calls to the management canister: `stop_canister`, `install_code` (upgrade), and `start_canister`. Each step propagates its error upward:

```rust
runtime.stop_canister(canister_id).await
    .map_err(UpgradeLedgerSuiteError::StopCanisterError)?;
runtime.upgrade_canister(canister_id, wasm.to_bytes()).await
    .map_err(UpgradeLedgerSuiteError::UpgradeCanisterError)?;
runtime.start_canister(canister_id).await
    .map_err(UpgradeLedgerSuiteError::StartCanisterError)?;
``` [2](#0-1) 

If any of these calls fails for any archive (e.g., the archive is out of cycles, is in a bad state, or the management canister rejects the call), the `?` operator propagates the error and the loop exits immediately. All archives appearing later in the list are not upgraded.

The retry mechanism in `run_task` schedules the same `UpgradeLedgerSuite` task for retry:

```rust
let rerun_task_guard = scopeguard::guard(task.task_type.clone(), |task_type| {
    schedule_after(RETRY_FREQUENCY, task_type, &runtime);
});
``` [3](#0-2) 

The `UpgradeArchives` subtask has no internal state tracking which archive was last processed. On retry, it starts from the first archive in the list again. If that archive is permanently broken (e.g., out of cycles), the retry keeps failing on it, and all subsequent archives are never upgraded.

The `UpgradeLedgerSuiteError` classification marks `StopCanisterError`, `UpgradeCanisterError`, and `StartCanisterError` as recoverable, meaning the task will be retried indefinitely:

```rust
UpgradeLedgerSuiteError::StopCanisterError(_) => true,
UpgradeLedgerSuiteError::StartCanisterError(_) => true,
UpgradeLedgerSuiteError::UpgradeCanisterError(_) => true,
``` [4](#0-3) 

### Impact Explanation
When an NNS governance proposal triggers an upgrade of the ledger suite orchestrator with a new archive wasm hash, the orchestrator schedules `UpgradeLedgerSuite` tasks for all managed ERC-20 tokens (ckUSDC, ckUSDT, ckETH, etc.). If any archive canister for a given token fails to upgrade, all subsequent archives for that token are permanently blocked from being upgraded. This leaves the ledger suite in an inconsistent state where some archives run old code and others run new code, which can cause compatibility issues between the ledger and its archives. The README itself acknowledges that multiple archives can exist per token:

> stop/upgrade/start archive canister if a wasm hash was specified. This also involves contacting the ledger to see if any archive canisters were created. [5](#0-4) 

The comment in the code — `//We expect usually 0 or 1 archive, so a simple sequential strategy is good enough` — reflects an assumption that may not hold as ledger suites grow and accumulate multiple archive canisters over time.

### Likelihood Explanation
Medium. Archive canisters can run out of cycles if the `maybe_top_up` mechanism fails to top them up in time (e.g., due to high usage, a race condition, or a bug in the top-up mechanism). An archive canister that is out of cycles will reject `stop_canister` calls from the management canister. Additionally, archive canisters can be in a bad state due to bugs or unexpected conditions during a prior failed upgrade attempt (e.g., left in a stopped state). The `UpgradeArchives` subtask is triggered by NNS governance proposals, which are infrequent but high-stakes operations. The impact is significant when it occurs because it permanently blocks upgrades for all archives after the failing one until manual intervention.

### Recommendation
Replace the early-exit `?` with per-archive error handling that logs the failure and continues to the next archive:

```rust
let mut failed_archives = vec![];
for canister_id in archives {
    if let Err(e) = upgrade_canister::<Archive, _>(canister_id, compressed_wasm_hash, runtime).await {
        log!(ERROR, "[UpgradeArchives] Failed to upgrade archive {canister_id}: {e:?}. Skipping and continuing.");
        failed_archives.push((canister_id, e));
    }
}
if !failed_archives.is_empty() {
    return Err(UpgradeLedgerSuiteError::PartialArchiveUpgradeFailure(failed_archives));
}
Ok(())
```

Alternatively, track which archives have been successfully upgraded within the `UpgradeLedgerSuite` struct so that retries skip already-upgraded archives and do not re-attempt permanently failing ones indefinitely.

### Proof of Concept
1. An NNS governance proposal is submitted to upgrade the ledger suite orchestrator with a new archive wasm hash.
2. The orchestrator schedules `UpgradeLedgerSuite` tasks for all managed ERC-20 tokens (e.g., ckUSDC).
3. ckUSDC has accumulated two archive canisters (archive-1 and archive-2) due to high transaction volume.
4. The `UpgradeArchives` subtask begins iterating: it calls `upgrade_canister` for archive-1.
5. archive-1 is out of cycles; `stop_canister` is rejected by the management canister.
6. `UpgradeLedgerSuiteError::StopCanisterError` is returned; `?` propagates it; the loop exits.
7. archive-2 is never upgraded.
8. `run_task` marks the error as recoverable and schedules a retry after `RETRY_FREQUENCY` (5 seconds).
9. On retry, the `UpgradeArchives` subtask starts from archive-1 again; it still fails.
10. archive-2 remains on the old wasm indefinitely, running code incompatible with the upgraded ledger, until an operator manually tops up archive-1 or removes it from the managed list.

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L196-198)
```rust
    let rerun_task_guard = scopeguard::guard(task.task_type.clone(), |task_type| {
        schedule_after(RETRY_FREQUENCY, task_type, &runtime);
    });
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L414-418)
```rust
                //We expect usually 0 or 1 archive, so a simple sequential strategy is good enough.
                for canister_id in archives {
                    upgrade_canister::<Archive, _>(canister_id, compressed_wasm_hash, runtime)
                        .await?;
                }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L674-677)
```rust
            UpgradeLedgerSuiteError::StopCanisterError(_) => true,
            UpgradeLedgerSuiteError::StartCanisterError(_) => true,
            UpgradeLedgerSuiteError::UpgradeCanisterError(_) => true,
            UpgradeLedgerSuiteError::DiscoverArchivesError(e) => e.is_recoverable(),
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

**File:** rs/ethereum/ledger-suite-orchestrator/README.adoc (L196-202)
```text
The orchestrator verifies that the wasm hashes when present are known and then tries to do the following for every managed ERC-20 token on a timer:

. stop/upgrade/start index canister if a wasm hash was specified;
. stop/upgrade/start ledger if a wasm hash was specified;
. stop/upgrade/start archive canister if a wasm hash was specified. This also involves contacting the ledger to see if any archive canisters were created.

In case any operation fails, a retry will be initiated on the next timer, starting from the previously failing step.
```
