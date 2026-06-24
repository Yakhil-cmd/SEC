### Title
Unbounded `join_all` over All Managed Principals in `maybe_top_up` Can Permanently Lock the Ledger Suite Orchestrator's Cycle Top-Up Task - (File: rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs)

### Summary
The `maybe_top_up` periodic task in the Ledger Suite Orchestrator (LSO) collects every managed principal — ledger, index, and all archive canisters for every ERC-20 token — and fans out a `future::join_all` of concurrent inter-canister `canister_cycles` calls over the entire unbounded set. Because archive canisters are added automatically as ledgers fill up (driven by ordinary user transactions) and there is no mechanism to remove managed principals, the set grows without bound. Once the set is large enough to exhaust the IC's per-canister outstanding-callback budget or the per-message instruction limit, the task traps on every invocation and the orchestrator can never top up its managed canisters again.

### Finding Description
`maybe_top_up` is scheduled as a periodic `Task::MaybeTopUp` that fires every hour via the global timer.

```rust
// rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs  lines 713-758
async fn maybe_top_up<R: CanisterRuntime>(runtime: &R) -> Result<(), TaskError> {
    let managed_principals: BTreeSet<_> =
        read_state(|s| s.all_managed_principals().cloned().collect()); // ← collects ALL
    ...
    let results = future::join_all(
        managed_principals
            .iter()
            .map(|p| runtime.canister_cycles(*p)),  // ← one call per principal
    )
    .await;
```

`all_managed_principals` is defined as:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs  lines 553-556
pub fn all_managed_principals(&self) -> impl Iterator<Item = &Principal> {
    self.all_managed_canisters_iter()
        .flat_map(|(_, canisters)| canisters.principals_iter())
}
```

This yields the ledger, index, and every archive for every managed ERC-20 token. Archives are discovered and appended to the managed set by the `discover_archives` task, which calls `icrc3_get_archives` on each ledger and records the results:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs  lines 1150-1192
async fn discover_archives<R: CanisterRuntime, F: Fn(&TokenId) -> bool>(
    selector: F,
    runtime: &R,
) -> Result<(), DiscoverArchivesError> {
    ...
    mutate_state(|s| s.record_archives(&token_id, archives.into_iter().collect()));
```

There is no corresponding `remove_archive` or `unmanage_canister` operation anywhere in the codebase. The `State` struct has no removal path for managed canisters:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs  lines 518-530
pub struct State {
    managed_canisters: ManagedCanisters,
    ...
}
```

Each ICRC-1 ledger is configured to archive every 2 000 blocks into a new archive canister:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs  lines 957-960
ArchiveOptions {
    trigger_threshold: 2_000,
    num_blocks_to_archive: 1_000,
    ...
}
```

So a single busy ledger spawns one new archive canister per 2 000 transactions. With many ERC-20 tokens and active usage, the managed-principal count grows continuously.

### Impact Explanation
When `managed_principals` is large enough, the `future::join_all` call in `maybe_top_up` will either:

1. **Exhaust the per-canister outstanding-callback budget** — the IC enforces a hard limit on the number of simultaneous outstanding inter-canister calls a canister may have. Attempting to open more calls than this limit causes the excess calls to be rejected, causing the task to return an error and be rescheduled, but the condition never clears.
2. **Exceed the per-message instruction limit** — constructing and dispatching N concurrent calls consumes O(N) instructions in a single message execution. Once N is large enough, the message traps before all calls are sent; the state is rolled back and the task is rescheduled into the same failing state indefinitely.

In either case the `MaybeTopUp` task enters a permanent failure loop. The orchestrator stops topping up all managed canisters. Ledger, index, and archive canisters drain their cycle balances, become frozen, and eventually stop serving user requests (transfers, queries, archive reads). The orchestrator itself cannot recover without an NNS upgrade that rewrites the task logic.

### Likelihood Explanation
The growth path is partially unprivileged: NNS governance must approve each new ERC-20 token (a trusted but routine operation), but once a token is live, **any user** can trigger archive creation simply by making transactions. The LSO comment in the code acknowledges the expected growth: `"We expect usually 0 or 1 archive"` — but places no cap. With the current mainnet deployment managing multiple ckERC20 tokens and active trading, archive counts will grow naturally over time without any adversarial intent. The NNS signers are unlikely to be aware of the per-task callback-budget constraint.

### Recommendation
1. **Paginate `maybe_top_up`**: process managed principals in bounded batches (e.g., 50 at a time), persisting a cursor in state so each timer invocation advances the cursor rather than restarting from the full set.
2. **Add a `remove_managed_canister` / `untrack_archive` operation** so that decommissioned or excess archives can be removed from the managed set.
3. **Cap the number of concurrent inter-canister calls** in `maybe_top_up` and `discover_archives` to a safe constant (e.g., 100) using a chunked `join_all` or a semaphore-limited stream.

### Proof of Concept
1. Deploy the LSO on a test subnet.
2. Add N ERC-20 tokens via NNS proposals (N ≥ 50 is sufficient for a realistic test).
3. For each token, submit enough ICRC-1 transfers to trigger archive creation (2 000 transfers per archive).
4. Wait for `discover_archives` to record the new archives in state.
5. Observe that the next `MaybeTopUp` timer invocation either traps (instruction limit) or returns a callback-budget error, and that subsequent invocations continue to fail.
6. Confirm that managed canisters' cycle balances are no longer being replenished. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L105-108)
```rust
            self.deadline_by_task
                .insert(old_task.task_type.clone(), execute_at_ns);
            self.queue.insert(
                TaskExecution {
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L392-419)
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
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L713-758)
```rust
async fn maybe_top_up<R: CanisterRuntime>(runtime: &R) -> Result<(), TaskError> {
    let managed_principals: BTreeSet<_> =
        read_state(|s| s.all_managed_principals().cloned().collect());
    if managed_principals.is_empty() {
        log!(INFO, "[maybe_top_up]: No managed canisters to top-up");
        return Ok(());
    }
    let cycles_management = read_state(|s| s.cycles_management().clone());
    let minimum_orchestrator_cycles =
        cycles_to_u128(cycles_management.minimum_orchestrator_cycles());
    let minimum_monitored_canister_cycles =
        cycles_to_u128(cycles_management.minimum_monitored_canister_cycles());
    let top_up_amount = cycles_to_u128(cycles_management.cycles_top_up_increment.clone());
    log!(
        INFO,
        "[maybe_top_up]: Managed canisters {}. \
        Cycles management: {cycles_management:?}. \
    Required amount of cycles for orchestrator to be able to top-up: {minimum_orchestrator_cycles}. \
    Monitored canister minimum target cycles balance {minimum_monitored_canister_cycles}",
        display_iter(&managed_principals)
    );

    let mut orchestrator_cycle_balance = match runtime.canister_cycles(runtime.id()).await {
        Ok(balance) => balance,
        Err(e) => {
            log!(
                INFO,
                "[maybe_top_up] failed to get orchestrator status, with error: {:?}",
                e
            );
            return Err(TaskError::CanisterStatusError(e));
        }
    };
    if orchestrator_cycle_balance < minimum_orchestrator_cycles {
        return Err(TaskError::InsufficientCyclesToTopUp {
            required: minimum_orchestrator_cycles,
            available: orchestrator_cycle_balance,
        });
    }

    let results = future::join_all(
        managed_principals
            .iter()
            .map(|p| runtime.canister_cycles(*p)),
    )
    .await;
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1150-1192)
```rust
async fn discover_archives<R: CanisterRuntime, F: Fn(&TokenId) -> bool>(
    selector: F,
    runtime: &R,
) -> Result<(), DiscoverArchivesError> {
    let ledgers: BTreeMap<_, _> = read_state(|s| {
        s.all_managed_canisters_iter()
            .filter(|(token, _)| selector(token))
            .filter_map(|(token_id, canisters)| {
                canisters
                    .ledger_canister_id()
                    .cloned()
                    .map(|ledger_id| (token_id, ledger_id))
            })
            .collect()
    });
    if ledgers.is_empty() {
        return Ok(());
    }
    log!(
        INFO,
        "[discover_archives]: discovering archives for {:?}",
        ledgers
    );
    let results = future::join_all(
        ledgers
            .values()
            .map(|p| call_ledger_icrc3_get_archives(*p, runtime)),
    )
    .await;
    let mut errors: Vec<(TokenId, Principal, DiscoverArchivesError)> = Vec::new();
    for ((token_id, ledger), result) in ledgers.into_iter().zip(results) {
        match result {
            Ok(archives) => {
                //order is not guaranteed by the API of icrc3_get_archives.
                let archives: BTreeSet<_> = archives.into_iter().map(|a| a.canister_id).collect();
                log!(
                    DEBUG,
                    "[discover_archives]: archives for ERC-20 token {:?} with ledger {}: {}",
                    token_id,
                    ledger,
                    display_iter(&archives)
                );
                mutate_state(|s| s.record_archives(&token_id, archives.into_iter().collect()));
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L518-530)
```rust
#[derive(Clone, PartialEq, Debug, Deserialize, Serialize)]
pub struct State {
    managed_canisters: ManagedCanisters,
    #[serde(default)]
    completed_upgrades: BTreeMap<Principal, CanisterUpgrade>,
    cycles_management: CyclesManagement,
    more_controller_ids: Vec<Principal>,
    minter_id: Option<Principal>,
    /// Locks preventing concurrent execution timer tasks
    pub active_tasks: BTreeSet<Task>,
    #[serde(default)]
    ledger_suite_version: Option<LedgerSuiteVersion>,
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L553-556)
```rust
    pub fn all_managed_principals(&self) -> impl Iterator<Item = &Principal> {
        self.all_managed_canisters_iter()
            .flat_map(|(_, canisters)| canisters.principals_iter())
    }
```
