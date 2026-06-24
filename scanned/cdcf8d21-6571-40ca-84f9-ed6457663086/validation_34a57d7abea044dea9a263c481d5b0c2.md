### Title
Sequential Archive Canister Upgrade Loop Lacks Fail-Safe Handling — (`rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`)

---

### Summary

The `UpgradeLedgerSuiteSubtask::UpgradeArchives` task in the ckERC20 Ledger Suite Orchestrator iterates over all archive canisters for a given ERC-20 token using a sequential loop with `?`-propagation. If any single archive canister upgrade call fails (e.g., `stop_canister`, `upgrade_canister`, or `start_canister` is rejected by the management canister), the entire subtask aborts immediately. The retry mechanism re-enters the loop from the beginning, meaning a permanently-failing first archive permanently blocks the upgrade of all subsequent archives for that token.

---

### Finding Description

In `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`, the `UpgradeArchives` subtask executes:

```rust
for canister_id in archives {
    upgrade_canister::<Archive, _>(canister_id, compressed_wasm_hash, runtime)
        .await?;
}
``` [1](#0-0) 

The `?` operator propagates the first error immediately, abandoning all remaining archive canisters in the list. The orchestrator's task scheduler retries the entire `UpgradeArchives` subtask on the next timer tick, but always restarts from the head of the `archives` list. If the first archive canister is in a permanently unresponsive state, the retry loop will never reach subsequent archives.

The `upgrade_canister` helper itself calls three management-canister operations in sequence, each with `?`:

```rust
runtime.stop_canister(canister_id).await
    .map_err(UpgradeLedgerSuiteError::StopCanisterError)?;
runtime.upgrade_canister(canister_id, wasm.to_bytes()).await
    .map_err(UpgradeLedgerSuiteError::UpgradeCanisterError)?;
runtime.start_canister(canister_id).await
    .map_err(UpgradeLedgerSuiteError::StartCanisterError)?;
``` [2](#0-1) 

If `stop_canister` succeeds but `upgrade_canister` fails (e.g., the archive's `post_upgrade` hook panics, or the orchestrator is temporarily low on cycles), the archive is left in a **stopped** state. Every subsequent retry will stop it again, re-attempt the failing upgrade, and leave it stopped again — permanently denying service from that archive canister.

The orchestrator's own documentation acknowledges the retry-on-failure design:

> "In case any operation fails, a retry will be initiated on the next timer, starting from the previously failing step." [3](#0-2) 

But "previously failing step" means the same failing archive, not the next one.

---

### Impact Explanation

1. **Stuck archive upgrade**: If a ckERC20 token accumulates two or more archive canisters (possible for high-volume tokens such as ckUSDC or ckUSDT) and the first archive enters a permanently failing state, all subsequent archives will never receive the upgrade. They continue running the old wasm indefinitely, including any security-relevant bugs fixed in the new version.

2. **Archive canister left stopped**: If `stop_canister` succeeds but `upgrade_canister` fails, the archive is left stopped on every retry cycle. Users querying historical transactions routed to that archive receive errors. Because the orchestrator controls the archive and no other actor can restart it without a governance proposal, the archive is effectively bricked until a new NNS upgrade proposal is submitted.

3. **Scope**: The failure is per-token (each token has its own `UpgradeLedgerSuite` task), so it does not block other tokens' upgrades. However, for the affected token, the archive upgrade pipeline is permanently stalled.

---

### Likelihood Explanation

The code comment acknowledges the assumption: *"We expect usually 0 or 1 archive, so a simple sequential strategy is good enough."* [4](#0-3) 

For high-volume ckERC20 tokens (ckUSDC, ckUSDT), multiple archives are realistic over time. Failure triggers include:

- **Orchestrator cycles exhaustion**: If the orchestrator's cycle balance drops below the cost of a management-canister call during the upgrade window, `stop_canister` or `start_canister` will be rejected. A chain-fusion user can accelerate this by submitting many ckERC20 transfers, increasing the orchestrator's operational load and cycle burn rate.
- **Archive `post_upgrade` panic**: A bug in the new archive wasm causes `upgrade_canister` to fail, leaving the archive stopped. This is not attacker-controlled but is a realistic operational failure.
- **Management canister transient rejection**: Any transient platform error during the three-step stop/upgrade/start sequence leaves the archive in an intermediate state that the retry loop cannot recover from without manual intervention.

---

### Recommendation

Replace the early-exit loop with a fail-safe pattern that logs errors and continues to the next archive:

```rust
let mut errors = vec![];
for canister_id in archives {
    if let Err(e) = upgrade_canister::<Archive, _>(canister_id, compressed_wasm_hash, runtime).await {
        log!(ERROR, "Failed to upgrade archive {}: {:?}", canister_id, e);
        errors.push((canister_id, e));
    }
}
if !errors.is_empty() {
    return Err(UpgradeLedgerSuiteError::PartialArchiveUpgradeFailure(errors));
}
Ok(())
```

Additionally, the `upgrade_canister` function should handle the intermediate stopped-but-not-upgraded state: before attempting `stop_canister`, check the canister's current status and skip the stop step if it is already stopped.

---

### Proof of Concept

1. Deploy the ckERC20 Ledger Suite Orchestrator with a token (e.g., ckUSDC) and allow the ledger to accumulate enough transactions to spawn two archive canisters (`archive_0`, `archive_1`).
2. Cause `archive_0` to enter a state where `upgrade_canister` will fail (e.g., install a wasm with a panicking `post_upgrade` hook out-of-band, or exhaust the orchestrator's cycles below the management-canister call threshold).
3. Submit an NNS upgrade proposal for the orchestrator with a new `archive_compressed_wasm_hash`.
4. Observe via `get_orchestrator_info` that `archive_0` is left in a stopped state and `archive_1` retains the old wasm hash indefinitely across all timer ticks, confirming that the upgrade loop never reaches `archive_1`. [5](#0-4) [6](#0-5)

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

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1271-1313)
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
```

**File:** rs/ethereum/ledger-suite-orchestrator/README.adoc (L196-202)
```text
The orchestrator verifies that the wasm hashes when present are known and then tries to do the following for every managed ERC-20 token on a timer:

. stop/upgrade/start index canister if a wasm hash was specified;
. stop/upgrade/start ledger if a wasm hash was specified;
. stop/upgrade/start archive canister if a wasm hash was specified. This also involves contacting the ledger to see if any archive canisters were created.

In case any operation fails, a retry will be initiated on the next timer, starting from the previously failing step.
```
