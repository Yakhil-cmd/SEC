### Title
Single Archive Canister Failure Blocks Entire `UpgradeArchives` Subtask for a Token - (`File: rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`)

---

### Summary

In the Ledger Suite Orchestrator (LSO), the `UpgradeArchives` subtask iterates sequentially over all archive canisters for a given ckERC20 token and upgrades them one by one. If any single archive canister's upgrade fails (e.g., `stop_canister`, `upgrade_canister`, or `start_canister` returns an error), the entire `UpgradeArchives` subtask returns an error immediately via `?`, leaving all subsequent archive canisters in the list un-upgraded and the already-stopped-but-not-yet-started archive canister in a stopped state until the next retry.

---

### Finding Description

The `UpgradeLedgerSuiteSubtask::UpgradeArchives` arm in `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs` iterates over all archive canisters for a token sequentially:

```rust
//We expect usually 0 or 1 archive, so a simple sequential strategy is good enough.
for canister_id in archives {
    upgrade_canister::<Archive, _>(canister_id, compressed_wasm_hash, runtime)
        .await?;   // <-- early return on first failure
}
``` [1](#0-0) 

The `upgrade_canister` function itself performs three sequential inter-canister management calls — `stop_canister`, `upgrade_canister`, `start_canister` — each propagated with `?`:

```rust
runtime.stop_canister(canister_id).await
    .map_err(UpgradeLedgerSuiteError::StopCanisterError)?;
runtime.upgrade_canister(canister_id, wasm.to_bytes()).await
    .map_err(UpgradeLedgerSuiteError::UpgradeCanisterError)?;
runtime.start_canister(canister_id).await
    .map_err(UpgradeLedgerSuiteError::StartCanisterError)?;
``` [2](#0-1) 

When the `UpgradeArchives` subtask fails, the error propagates up through `upgrade_ledger_suite`, which does **not** reschedule the remaining subtasks:

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
``` [3](#0-2) 

On error, the `?` causes the function to return `Err(...)` without calling `schedule_now`, so the remaining subtasks (including the rest of the archive list within the same `UpgradeArchives` subtask) are dropped. The task scheduler then decides whether to retry based on `is_recoverable`:

```rust
UpgradeLedgerSuiteError::StopCanisterError(_) => true,
UpgradeLedgerSuiteError::StartCanisterError(_) => true,
UpgradeLedgerSuiteError::UpgradeCanisterError(_) => true,
``` [4](#0-3) 

On retry, the `UpgradeArchives` subtask restarts from the **beginning of the archive list** (index 0), not from the failing archive. This means:

1. A permanently broken archive canister (e.g., one that consistently rejects `stop_canister` with a non-recoverable error) causes `UpgradeLedgerSuiteError::StopCanisterError` which is classified as `is_recoverable = true`, so the task retries indefinitely — but always fails at the same archive, never upgrading any subsequent archives.
2. If the failure occurs mid-loop (archive N fails after archives 0..N-1 were already stopped and upgraded), archive N is left in a stopped state until the next retry, which restarts from archive 0 again.

The analogous pattern in `discover_archives` (called with `select_all()` for the periodic `Task::DiscoverArchives`) also returns only the first error when multiple ledgers fail, but at least it processes all ledgers in parallel and records successful results before returning the error:

```rust
let first_error = errors.swap_remove(0);
return Err(first_error.2);
``` [5](#0-4) 

The `UpgradeArchives` loop has no such partial-success recording — it simply aborts on the first failure.

---

### Impact Explanation

- **Upgrade DOS**: If any single archive canister for a ckERC20 token (e.g., ckUSDC, ckUSDT) is in a state that causes a persistent upgrade failure (e.g., the archive canister is trapped, out of cycles, or its `canister_pre_upgrade` hook panics), the entire `UpgradeArchives` subtask for that token will retry indefinitely without ever upgrading the remaining archives.
- **Canister left stopped**: If the failure occurs after `stop_canister` succeeds but before `start_canister` completes, the archive canister is left in a stopped state, making it unavailable for ledger block queries until the next retry succeeds.
- **Blocked NNS upgrade proposal effect**: An NNS upgrade proposal targeting archive canisters will appear to be "in progress" but will never complete for the affected token, silently leaving some archives on an old version.
- The LSO manages production ckERC20 tokens (ckUSDC, ckUSDT, etc.) on mainnet. The `UpgradeArchives` subtask is triggered by NNS proposals. [6](#0-5) 

---

### Likelihood Explanation

- Archive canisters are spawned automatically by the ledger when a block threshold is crossed. As ckERC20 tokens accumulate transaction history, multiple archives per token become common.
- An archive canister can fail to stop/upgrade if it is out of cycles, if its `canister_pre_upgrade` hook panics due to a bug in the current version, or if the management canister call is rejected for any transient reason.
- The LSO comment itself acknowledges the assumption: `"We expect usually 0 or 1 archive, so a simple sequential strategy is good enough."` — but this assumption breaks as tokens mature and accumulate multiple archives. [7](#0-6) 

---

### Recommendation

1. **Continue on failure**: Instead of using `?` inside the archive loop, collect errors and continue upgrading remaining archives. Return the first error (or all errors) only after attempting all archives — mirroring the pattern already used in `discover_archives`.
2. **Track per-archive progress**: Record which archives have been successfully upgraded within the `UpgradeArchives` subtask state so that retries resume from the failing archive rather than restarting from the beginning.
3. **Separate tasks per archive**: Decompose `UpgradeArchives` into one `Task::UpgradeLedgerSuite` subtask per archive canister, so a failure in one does not block others.

---

### Proof of Concept

Given a ckUSDC token with two archive canisters `[archive_A, archive_B]`:

1. NNS proposal sets `archive_compressed_wasm_hash` → LSO schedules `UpgradeLedgerSuite` with subtask `UpgradeArchives { token_id: ckUSDC, ... }`.
2. `UpgradeArchives` executes: calls `upgrade_canister(archive_A, ...)`.
3. `stop_canister(archive_A)` succeeds → archive_A is now stopped.
4. `upgrade_canister(archive_A, wasm)` returns `Err(UpgradeCanisterError(...))` (e.g., archive_A's pre-upgrade hook panics).
5. The `?` propagates the error; `archive_B` is never touched.
6. `upgrade_ledger_suite` returns `Err(...)` without calling `schedule_now` for remaining subtasks.
7. The task scheduler sees `is_recoverable = true` and reschedules the full `UpgradeArchives` subtask.
8. On retry, the loop starts again from `archive_A` — which is now stopped and still failing — so `archive_B` is never upgraded.
9. `archive_A` remains stopped indefinitely, blocking ledger block queries to that archive.

The test `should_discover_archive_and_return_first_error` in `rs/ethereum/ledger-suite-orchestrator/src/scheduler/tests.rs` confirms the analogous behavior in `discover_archives` (partial success + first error returned), but no equivalent resilience test exists for `UpgradeArchives` with a mid-loop failure. [8](#0-7)

### Citations

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

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/tests.rs (L658-713)
```rust
    #[tokio::test]
    async fn should_discover_archive_and_return_first_error() {
        init_state();
        let (dai, dai_ledger) = (dai(), Principal::from_slice(&[4_u8; 29]));
        let (usdc, usdc_ledger) = (usdc(), Principal::from_slice(&[5_u8; 29]));
        let (usdt, usdt_ledger) = (usdt(), Principal::from_slice(&[6_u8; 29]));
        mutate_state(|s| {
            s.record_new_erc20_token(dai.clone(), dai_metadata());
            s.record_created_canister::<Ledger>(&dai, dai_ledger);

            s.record_new_erc20_token(usdc.clone(), usdc_metadata());
            s.record_created_canister::<Ledger>(&usdc, usdc_ledger);

            s.record_new_erc20_token(usdt.clone(), usdt_metadata());
            s.record_created_canister::<Ledger>(&usdt, usdt_ledger);
        });

        let mut runtime = MockCanisterRuntime::new();
        let first_error = CallError {
            method: "dai error".to_string(),
            reason: Reason::OutOfCycles,
        };
        expect_call_canister_icrc3_get_archives(&mut runtime, dai_ledger, Err(first_error.clone()));
        let usdc_archive = Principal::from_slice(&[7_u8; 29]);
        expect_call_canister_icrc3_get_archives(
            &mut runtime,
            usdc_ledger,
            Ok(vec![ICRC3ArchiveInfo {
                canister_id: usdc_archive,
                start: 0_u8.into(),
                end: 1_u8.into(),
            }]),
        );
        expect_call_canister_icrc3_get_archives(
            &mut runtime,
            usdt_ledger,
            Err(CallError {
                method: "usdt error".to_string(),
                reason: Reason::OutOfCycles,
            }),
        );

        let discover_archives_task = TaskExecution {
            task_type: Task::DiscoverArchives,
            execute_at_ns: 0,
        };
        assert_eq!(
            discover_archives_task.execute(&runtime).await,
            Err(TaskError::DiscoverArchivesError(
                DiscoverArchivesError::InterCanisterCallError(first_error)
            ))
        );
        assert_eq!(archives_from_state(&dai), vec![]);
        assert_eq!(archives_from_state(&usdc), vec![usdc_archive]);
        assert_eq!(archives_from_state(&usdt), vec![]);
    }
```
