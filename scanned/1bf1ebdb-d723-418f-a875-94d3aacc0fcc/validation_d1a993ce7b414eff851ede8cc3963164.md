### Title
Unbounded Concurrent Inter-Canister Calls in `maybe_top_up` and `discover_archives` Periodic Timers Cause Cycles Exhaustion - (File: `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`)

### Summary
The Ledger Suite Orchestrator's two periodic timer tasks — `maybe_top_up` and `discover_archives` — each fire one concurrent inter-canister call per managed canister/ledger using `future::join_all()` with no upper bound. As the number of managed ERC-20 tokens and their archive canisters grows, these hourly timers fan out an unbounded number of concurrent inter-canister calls in a single execution, risking cycles exhaustion and disruption of the entire ckERC20 top-up and archive-discovery infrastructure.

### Finding Description

**`maybe_top_up`** (`rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`, lines 713–813) collects every managed principal — ledger + index + all archive canisters for every ERC-20 token — and fires one concurrent `canister_cycles` call per principal with no cap:

```rust
let results = future::join_all(
    managed_principals
        .iter()
        .map(|p| runtime.canister_cycles(*p)),
)
.await;
```

`all_managed_principals()` expands to all ledger + index + archive principals across all tokens. Archives grow unboundedly as ledgers fill up (each archive holds a fixed number of transactions; new ones are spawned automatically by the ledger).

**`discover_archives`** (lines 1150–1208) fires one concurrent `icrc3_get_archives` call per managed ledger with no cap:

```rust
let results = future::join_all(
    ledgers
        .values()
        .map(|p| call_ledger_icrc3_get_archives(*p, runtime)),
)
.await;
```

Both tasks are marked `is_periodic() → true` and are rescheduled every hour via `schedule_after(ONE_HOUR, ...)` inside `run_task`. [1](#0-0) 

The `Task::MaybeTopUp` and `Task::DiscoverArchives` periodic scheduling is confirmed here: [2](#0-1) 

The unbounded `join_all` in `maybe_top_up`: [3](#0-2) 

The unbounded `join_all` in `discover_archives`: [4](#0-3) 

`all_managed_principals()` iterates all canisters including archives: [5](#0-4) 

### Impact Explanation

1. **Cycles exhaustion**: Every inter-canister call consumes cycles from the orchestrator. With N managed principals (N = tokens × (2 + avg\_archives\_per\_token)), each hourly `maybe_top_up` execution burns cycles proportional to N. As archives accumulate, N grows without bound, and the orchestrator can exhaust its cycles budget in a single timer execution — the very opposite of its intended purpose.

2. **IC per-canister in-flight call limit**: The IC enforces a hard limit on the number of in-flight calls from a single canister per round. If N exceeds this limit, excess `call_perform` invocations fail synchronously, causing those futures in `join_all` to return errors. Managed canisters whose status calls fail are silently skipped, leaving them without top-up.

3. **Cascading ckERC20 failure**: If the orchestrator's cycles are exhausted by the overhead of firing N concurrent calls, it cannot top up its managed ledger/index/archive canisters. Those canisters may then run out of cycles, halting ckERC20 minting, burning, and transaction archiving across the entire chain-fusion ecosystem.

### Likelihood Explanation

The Ledger Suite Orchestrator currently manages over 20 ERC-20 tokens (ckUSDC, ckUSDT, ckLINK, ckSHIB, etc.), each with a ledger, index, and a growing number of archive canisters. This is confirmed by the integration tests and upgrade proposals in the repository. [6](#0-5) 

As ckERC20 adoption grows and ledgers accumulate transactions, archives are spawned automatically. The number of managed principals grows monotonically. No code-level cap exists. The vulnerability worsens over time through normal system operation — no adversarial action is required.

### Recommendation

Introduce a configurable batch size limit (e.g., 50 or 100) on the number of concurrent inter-canister calls in both `maybe_top_up` and `discover_archives`. Process managed principals/ledgers in sequential batches rather than firing all calls simultaneously via a single `join_all`. This directly mirrors the original report's recommendation to aggregate or cap per-item actions when the list grows large.

### Proof of Concept

**Step 1**: The orchestrator manages N ERC-20 tokens, each with a ledger, index, and K archive canisters. Total managed principals = N × (2 + K).

**Step 2**: Every hour, `run_task` executes `Task::MaybeTopUp`: [2](#0-1) 

**Step 3**: `maybe_top_up` collects all managed principals and fires N×(2+K) concurrent `canister_cycles` calls with no cap: [7](#0-6) 

**Step 4**: Simultaneously, `Task::DiscoverArchives` fires N concurrent `icrc3_get_archives` calls with no cap: [8](#0-7) 

**Step 5**: With a sufficiently large N (achievable through legitimate NNS governance proposals adding ERC-20 tokens, or through natural archive growth), the orchestrator exhausts its cycles budget or hits the IC's per-canister call limit, causing the top-up and archive-discovery mechanisms to fail for all managed canisters simultaneously.

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L64-73)
```rust
impl Task {
    fn is_periodic(&self) -> bool {
        match self {
            Task::InstallLedgerSuite(_) => false,
            Task::MaybeTopUp => true,
            Task::NotifyErc20Added { .. } => false,
            Task::DiscoverArchives => true,
            Task::UpgradeLedgerSuite(_) => false,
        }
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L185-195)
```rust
async fn run_task<R: CanisterRuntime>(task: TaskExecution, runtime: R) {
    const RETRY_FREQUENCY: Duration = Duration::from_secs(5);
    const ONE_HOUR: Duration = Duration::from_secs(60 * 60);

    if task.task_type.is_periodic() {
        schedule_after(ONE_HOUR, task.task_type.clone(), &runtime);
    }
    let _guard = match crate::guard::TimerGuard::new(task.task_type.clone()) {
        Some(guard) => guard,
        None => return,
    };
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

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L1150-1178)
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
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L553-556)
```rust
    pub fn all_managed_principals(&self) -> impl Iterator<Item = &Principal> {
        self.all_managed_canisters_iter()
            .flat_map(|(_, canisters)| canisters.principals_iter())
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/README.adoc (L207-213)
```text
== Cycles top-up of managed ledger suites

On a timer, the ledger suite orchestrator tops up all managed canisters using a simple threshold strategy. The exact threshold and the top-up amount is specified in the ledger suite orchestrator initialization argument `CyclesManagement`. The topping-up strategy is as follows:

. The ledger suite orchestrator is monitored by the cycles monitor canister. The orchestrator will need a fairly big chunk of cycles and an alert will be fired when it does not have enough cycles.
. On a timer, it ensures that each managed canister has a cycles amount above the hard-coded threshold. This involves also contacting the ledger to see if any archive canisters were created, which is done on a separate timer.
. The threshold is set high enough so that the ledger always has sufficiently many cycles to be able to spawn a new archive canister and that all canisters have sufficiently many cycles to be able to be upgraded at any time.
```
