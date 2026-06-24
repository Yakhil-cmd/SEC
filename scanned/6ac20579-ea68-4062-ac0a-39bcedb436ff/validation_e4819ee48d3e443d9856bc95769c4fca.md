### Title
Sequential Archive Upgrade Loop Fails Atomically, Leaving Archive Canisters Stopped - (File: `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`)

### Summary

The `UpgradeLedgerSuiteSubtask::UpgradeArchives` handler in the Ledger Suite Orchestrator (LSO) iterates over all archive canisters for a given ckERC20 token sequentially. Each iteration calls `upgrade_canister`, which internally performs `stop_canister → upgrade_canister → start_canister`. If any single archive canister fails at any step, the `?` operator propagates the error immediately, aborting the loop. Any archive that was already stopped but not yet restarted is left in a permanently stopped state until the next retry succeeds. If the failure is persistent (e.g., a bad WASM, an out-of-cycles archive, or a management canister rejection), all remaining archives are never upgraded and the stopped archive remains inaccessible indefinitely.

### Finding Description

In `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`, the `UpgradeArchives` subtask executes:

```rust
//We expect usually 0 or 1 archive, so a simple sequential strategy is good enough.
for canister_id in archives {
    upgrade_canister::<Archive, _>(canister_id, compressed_wasm_hash, runtime)
        .await?;
}
``` [1](#0-0) 

The `?` on line 417 means that if `upgrade_canister` returns any `UpgradeLedgerSuiteError` (e.g., `StopCanisterError`, `UpgradeCanisterError`, or `StartCanisterError`), the entire subtask returns immediately. The `upgrade_canister` function performs three sequential management calls: `stop_canister`, then `upgrade_canister`, then `start_canister`. If `stop_canister` succeeds but `upgrade_canister` fails, the archive canister is left in a stopped state. The error variants are all marked recoverable:

```rust
UpgradeLedgerSuiteError::StopCanisterError(_) => true,
UpgradeLedgerSuiteError::StartCanisterError(_) => true,
UpgradeLedgerSuiteError::UpgradeCanisterError(_) => true,
``` [2](#0-1) 

This causes the task to be rescheduled. However, the retry restarts the `UpgradeArchives` subtask from the beginning of the archives list, not from the failing archive. Archives that were already successfully upgraded are re-upgraded (idempotent), but the stopped archive remains stopped until the upgrade call succeeds. If the failure is persistent (e.g., the new WASM is incompatible, or the archive is out of cycles), the archive stays stopped across all retries.

The `upgrade_ledger_suite` function executes one subtask per timer invocation and reschedules the remaining subtasks:

```rust
if let Some(subtask) = upgrade_ledger_suite.next() {
    subtask.execute(runtime).await?;
    if upgrade_ledger_suite.len() > 0 {
        schedule_now(Task::UpgradeLedgerSuite(upgrade_ledger_suite), runtime);
    }
}
``` [3](#0-2) 

When `subtask.execute` fails, the `?` on line 1235 propagates the error and `schedule_now` is never called, so the remaining subtasks (including the `UpgradeArchives` subtask itself with its remaining archives) are dropped. The task queue loses the progress made within the `UpgradeArchives` loop.

A secondary instance of the same pattern exists in `discover_archives`, which fans out `icrc3_get_archives` calls to all managed ledgers and returns only the first error if any fail, discarding successful results for other tokens:

```rust
let first_error = errors.swap_remove(0);
return Err(first_error.2);
``` [4](#0-3) 

This blocks the `DiscoverArchives` subtask for all tokens if any single ledger's `icrc3_get_archives` call fails, preventing the `UpgradeArchives` subtask from ever being reached for any token.

### Impact Explanation

Archive canisters hold the historical transaction ledger for ckERC20 tokens (ckUSDC, ckUSDT, etc.). A stopped archive canister:

1. Cannot respond to `icrc3_get_blocks` queries, making historical transaction data inaccessible to users and wallets.
2. Blocks the ICRC-3 index canister from fetching older blocks, degrading the index's ability to serve account transaction history.
3. Remains stopped across all retry cycles if the root cause is persistent, with no automatic recovery path other than a new NNS upgrade proposal.

The LSO manages all ckERC20 ledger suites. A single broken archive canister for one token blocks the upgrade of all archives for that token indefinitely.

### Likelihood Explanation

The upgrade flow is triggered by a legitimate NNS governance proposal (no attacker required). Transient failures that can trigger this condition include:

- An archive canister running low on cycles at the moment `upgrade_canister` is called (the management canister rejects the call).
- A transient `OutOfCycles` or `TransientInternalError` from the management canister during the upgrade step.
- A WASM that passes validation but traps during `post_upgrade`, causing the management canister to return an error after `stop_canister` has already succeeded.

The comment in the code itself acknowledges the sequential strategy: *"We expect usually 0 or 1 archive, so a simple sequential strategy is good enough."* As ckERC20 tokens accumulate transaction volume, the number of archives per token grows, increasing the probability that at least one archive is in a degraded state during any given upgrade window. [5](#0-4) 

### Recommendation

1. **Continue on per-archive failure**: Replace the `?` propagation with per-archive error collection. Log failures and continue upgrading remaining archives. Re-queue only the failed archives for retry.
2. **Track upgrade progress within the subtask**: Persist which archives have already been successfully upgraded so that retries resume from the failing archive rather than restarting from the beginning.
3. **Ensure `start_canister` is always called after `stop_canister`**: If `upgrade_canister` fails after `stop_canister` succeeds, the retry logic should detect the stopped state and attempt `start_canister` before re-attempting the upgrade, preventing indefinite stopped state.
4. **For `discover_archives`**: Collect all errors and return them together, or continue processing successful results and only fail for the tokens that errored, rather than aborting on the first error.

### Proof of Concept

1. An NNS proposal is submitted and approved to upgrade the LSO with a new archive WASM hash.
2. The LSO schedules `Task::UpgradeLedgerSuite` for each managed ckERC20 token (e.g., ckUSDC).
3. The `UpgradeArchives` subtask begins iterating over ckUSDC's two archive canisters: `archive_1` and `archive_2`.
4. `upgrade_canister(archive_1, ...)` calls `stop_canister(archive_1)` — succeeds. `archive_1` is now stopped.
5. `upgrade_canister(archive_1, ...)` calls `upgrade_canister(archive_1, new_wasm)` — fails (e.g., `archive_1` is out of cycles, returning `OutOfCycles`).
6. The `?` on line 417 propagates `UpgradeLedgerSuiteError::UpgradeCanisterError(...)`. `archive_2` is never touched.
7. The error propagates through `upgrade_ledger_suite` (line 1235), which returns without calling `schedule_now`. The remaining subtasks are dropped.
8. The task scheduler marks the error as recoverable and reschedules the full `UpgradeArchives` subtask.
9. On retry, `upgrade_canister(archive_1, ...)` calls `stop_canister(archive_1)` again — `archive_1` is already stopped, so this may succeed or return a "is stopped" error (also recoverable).
10. The upgrade attempt fails again for the same reason. `archive_1` remains stopped.
11. This cycle repeats indefinitely. `archive_1` is stopped and inaccessible. `archive_2` is never upgraded. Users querying historical ckUSDC transactions that reside in `archive_1` receive errors. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L392-420)
```rust
            UpgradeLedgerSuiteSubtask::UpgradeArchives {
                token_id,
                compressed_wasm_hash,
            } => {
                let archives = read_state(|s| s.managed_canisters(token_id).cloned())
                    .ok_or(UpgradeLedgerSuiteError::TokenNotFound(token_id.clone()))?
                    .archives;
                if archives.is_empty() {
                    log!(
                        INFO,
                        "No archive canisters found for {:?}. Skipping upgrade of archives.",
                        token_id
                    );
                    return Ok(());
                }
                log!(
                    INFO,
                    "Upgrading archive canisters {} for {:?} to {}",
                    display_iter(&archives),
                    token_id,
                    compressed_wasm_hash
                );
                //We expect usually 0 or 1 archive, so a simple sequential strategy is good enough.
                for canister_id in archives {
                    upgrade_canister::<Archive, _>(canister_id, compressed_wasm_hash, runtime)
                        .await?;
                }
                Ok(())
            }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L674-677)
```rust
            UpgradeLedgerSuiteError::StopCanisterError(_) => true,
            UpgradeLedgerSuiteError::StartCanisterError(_) => true,
            UpgradeLedgerSuiteError::UpgradeCanisterError(_) => true,
            UpgradeLedgerSuiteError::DiscoverArchivesError(e) => e.is_recoverable(),
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1204-1205)
```rust
        let first_error = errors.swap_remove(0);
        return Err(first_error.2);
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1229-1241)
```rust
async fn upgrade_ledger_suite<R: CanisterRuntime>(
    upgrade_ledger_suite: &UpgradeLedgerSuite,
    runtime: &R,
) -> Result<(), UpgradeLedgerSuiteError> {
    let mut upgrade_ledger_suite = upgrade_ledger_suite.clone();
    if let Some(subtask) = upgrade_ledger_suite.next() {
        subtask.execute(runtime).await?;
        if upgrade_ledger_suite.len() > 0 {
            schedule_now(Task::UpgradeLedgerSuite(upgrade_ledger_suite), runtime);
        }
    }
    Ok(())
}
```
