### Title
Deferred Resource-Charge Lump-Sum Applied Immediately After DTS Execution Completes or Is Aborted, Causing Unexpected Canister Uninstallation - (File: rs/execution_environment/src/scheduler.rs)

### Summary
The Internet Computer scheduler intentionally skips resource-allocation charging for any canister that has a paused (DTS) execution. When the paused execution eventually completes or is forcibly aborted at a checkpoint, the entire accumulated unpaid duration is charged in a single lump sum. A canister whose balance is sufficient to survive periodic charging may be immediately uninstalled when the deferred lump-sum charge is applied, with no grace period for the canister controller to top up.

### Finding Description
`charge_canisters_for_resource_allocation_and_usage` in `rs/execution_environment/src/scheduler.rs` explicitly skips any canister that has a paused or aborted execution:

```rust
// Postpone charging for resources when a canister has a paused execution
// to avoid modifying the balance of a canister during an unfinished operation.
if canister.has_paused_execution_or_install_code() {
    return;
}
``` [1](#0-0) 

Because the skip also prevents updating `time_of_last_allocation_charge`, the field retains the timestamp from before the DTS execution began. When charging is finally allowed (after the execution finishes or is aborted), `duration_since_last_allocation_charge` returns the full wall-clock time elapsed since that old timestamp:

```rust
let duration_since_last_charge =
    canister.duration_since_last_allocation_charge(state_time);
canister.system_state.time_of_last_allocation_charge = state_time;
``` [2](#0-1) 

`duration_since_last_allocation_charge` is a simple subtraction with no cap:

```rust
Duration::from_nanos(
    current_time.as_nanos_since_unix_epoch().saturating_sub(
        self.system_state
            .time_of_last_allocation_charge
            .as_nanos_since_unix_epoch(),
    ),
)
``` [3](#0-2) 

At every checkpoint round, `finish_round` first aborts **all** paused executions, then immediately calls `charge_canisters_for_resource_allocation_and_usage`:

```rust
// Abort (some) paused executions.
self.finish_round(&mut final_state, current_round_type);

// Charge canisters after (some) paused executions were aborted.
self.charge_canisters_for_resource_allocation_and_usage(...)
``` [4](#0-3) 

`finish_round` on a checkpoint round calls `abort_all_paused_executions`, which converts every `PausedExecution` to `AbortedExecution`, clearing the `has_paused_execution_or_install_code()` guard:

```rust
ExecutionRoundType::CheckpointRound => {
    // Abort all paused execution before the checkpoint.
    abort_all_paused_executions(state, &self.exec_env, cost_schedule, &self.log);
}
``` [5](#0-4) 

The existing test `dont_charge_allocations_for_paused_canisters` confirms the deferral behavior and that the full accumulated duration is charged in one shot when the execution completes:

```rust
// The balance has changed for the canisters with paused execution/install code.
assert_balance_change(&test, paused_canister, duration_plus_one_second);
``` [6](#0-5) 

The codebase itself acknowledges the gap with an open TODO:

```rust
// TODO(DSM-103): Charge all canisters every N rounds / seconds (and otherwise
// do nothing). Ensure that paused execution canisters are charged eventually.
``` [7](#0-6) 

### Impact Explanation
A canister whose balance is just above the freezing threshold at the start of a long DTS execution may be uninstalled the moment the execution ends or is aborted at a checkpoint. The lump-sum charge covers the entire deferred period (potentially many seconds or minutes of compute/memory fees) and is applied atomically with no opportunity for the controller to top up first. Uninstallation destroys all canister state, terminates open call contexts, and permanently removes the Wasm module — a severe, irreversible outcome for the canister owner. The controller receives no advance warning because the balance appeared healthy throughout the DTS execution.

### Likelihood Explanation
Any canister developer can trigger this condition without any privileged access. A canister with a heartbeat or a large update call that spans multiple DTS slices will have its charging deferred. If the canister's balance is sized to cover normal periodic charges but not a multi-interval lump sum, the canister will be uninstalled at the next checkpoint. This is a realistic operational scenario for canisters with tight cycle budgets and long-running computations. The likelihood is **Medium**: it requires a specific balance/execution-duration combination, but no adversarial action — a legitimate canister developer can hit this inadvertently.

### Recommendation
1. **Update `time_of_last_allocation_charge` even when charging is deferred.** The timestamp should advance to the current round time even if the actual debit is postponed, so that when charging is finally applied the duration is bounded to at most one charge interval rather than the full DTS execution span.
2. **Cap `duration_since_last_allocation_charge`** to `duration_between_allocation_charges` when computing the lump-sum charge, preventing unbounded catch-up charges.
3. **Notify controllers** (via canister logs or a system-level event) when a canister's balance is approaching the level where a deferred lump-sum charge would trigger uninstallation.

### Proof of Concept
1. Create a canister with `compute_allocation = 1%` and a balance equal to exactly `freeze_threshold + 2 × duration_between_allocation_charges × idle_rate` (enough for two normal charge intervals).
2. Submit an update message whose Wasm body requires more instructions than `max_instructions_per_slice`, causing it to enter DTS and produce a `PausedExecution` task.
3. Advance wall-clock time by `3 × duration_between_allocation_charges` without triggering a checkpoint. During this time, `charge_canisters_for_resource_allocation_and_usage` skips the canister on every eligible round because `has_paused_execution_or_install_code()` returns `true`.
4. Trigger a `CheckpointRound`. `finish_round` calls `abort_all_paused_executions`, converting the task to `AbortedExecution`. Immediately after, `charge_canisters_for_resource_allocation_and_usage` runs; `duration_since_last_allocation_charge` returns `3 × duration_between_allocation_charges`; the lump-sum charge exceeds the canister's balance; `charge_canister_for_resource_allocation_and_usage` returns `Err`; `uninstall_canister` is called and the canister is destroyed.

The test `dont_charge_allocations_for_paused_canisters` already demonstrates steps 1–3 and the deferred-then-applied-in-full charging behavior at lines 384–406 of `rs/execution_environment/src/scheduler/tests/charging.rs`. [8](#0-7)

### Citations

**File:** rs/execution_environment/src/scheduler.rs (L846-847)
```rust
        // TODO(DSM-103): Charge all canisters every N rounds / seconds (and otherwise
        // do nothing). Ensure that paused execution canisters are charged eventually.
```

**File:** rs/execution_environment/src/scheduler.rs (L857-861)
```rust
            // Postpone charging for resources when a canister has a paused execution
            // to avoid modifying the balance of a canister during an unfinished operation.
            if canister.has_paused_execution_or_install_code() {
                return;
            }
```

**File:** rs/execution_environment/src/scheduler.rs (L864-866)
```rust
            let duration_since_last_charge =
                canister.duration_since_last_allocation_charge(state_time);
            canister.system_state.time_of_last_allocation_charge = state_time;
```

**File:** rs/execution_environment/src/scheduler.rs (L1094-1103)
```rust
            ExecutionRoundType::CheckpointRound => {
                state.metadata.heap_delta_estimate = NumBytes::new(0);
                // The set of compiled Wasms must be cleared when taking a
                // checkpoint to keep it in sync with the protobuf serialization
                // of `ReplicatedState` which doesn't store this field.
                state.metadata.expected_compiled_wasms = Arc::new(BTreeSet::new());

                // Abort all paused execution before the checkpoint.
                abort_all_paused_executions(state, &self.exec_env, cost_schedule, &self.log);
            }
```

**File:** rs/execution_environment/src/scheduler.rs (L1529-1540)
```rust
            // Abort (some) paused executions.
            self.finish_round(&mut final_state, current_round_type);

            // Charge canisters after (some) paused executions were aborted.
            {
                let _timer = self.metrics.round_finalization_charge.start_timer();
                self.charge_canisters_for_resource_allocation_and_usage(
                    &mut final_state,
                    current_round,
                    current_round_type,
                );
            }
```

**File:** rs/replicated_state/src/canister_state.rs (L123-130)
```rust
        Duration::from_nanos(
            current_time.as_nanos_since_unix_epoch().saturating_sub(
                self.system_state
                    .time_of_last_allocation_charge
                    .as_nanos_since_unix_epoch(),
            ),
        )
    }
```

**File:** rs/execution_environment/src/scheduler/tests/charging.rs (L335-407)
```rust
#[test]
fn dont_charge_allocations_for_paused_canisters() {
    const T0: Time = Time::from_nanos_since_unix_epoch(1_000_000_000);
    const INITIAL_CYCLES: Cycles = Cycles::new(10_000_000);
    const MEMORY_ALLOCATION: NumBytes = NumBytes::new(1 << 30);

    let mut test = SchedulerTestBuilder::new().build();

    let mut create_canister_with_memory_allocation = || -> CanisterId {
        test.create_canister_with(
            INITIAL_CYCLES,
            ComputeAllocation::zero(),
            MemoryAllocation::from(MEMORY_ALLOCATION),
            None,
            Some(T0),
            None,
        )
    };
    let canister = create_canister_with_memory_allocation();
    let paused_canister = create_canister_with_memory_allocation();
    let paused_install_canister = create_canister_with_memory_allocation();

    test.canister_state_mut(paused_canister)
        .system_state
        .task_queue
        .enqueue(ExecutionTask::PausedExecution {
            id: PausedExecutionId(0),
            input: CanisterMessageOrTask::Task(CanisterTask::Heartbeat),
        });
    test.canister_state_mut(paused_install_canister)
        .system_state
        .task_queue
        .enqueue(ExecutionTask::PausedInstallCode(PausedExecutionId(0)));

    let duration_between_allocation_charges = test.duration_between_allocation_charges();
    test.set_time(T0 + duration_between_allocation_charges);

    test.charge_for_resource_allocations();

    fn assert_balance_change(test: &SchedulerTest, canister: CanisterId, duration: Duration) {
        assert_eq!(
            test.canister_state(canister).system_state.balance(),
            INITIAL_CYCLES
                - test.memory_cost(MEMORY_ALLOCATION, duration).real()
                - test.canister_base_cost(MEMORY_ALLOCATION, duration).real()
        );
    }
    // Balance has changed for the canister with no paused execution.
    assert_balance_change(&test, canister, duration_between_allocation_charges);
    // Balance has not changed for the canisters with paused execution/install code.
    assert_balance_change(&test, paused_canister, Duration::from_secs(0));
    assert_balance_change(&test, paused_install_canister, Duration::from_secs(0));

    // One second later, the two long executions complete.
    let duration_plus_one_second = duration_between_allocation_charges + Duration::from_secs(1);
    test.set_time(T0 + duration_plus_one_second);
    test.canister_state_mut(paused_canister)
        .system_state
        .task_queue
        .pop_front();
    test.canister_state_mut(paused_install_canister)
        .system_state
        .task_queue
        .pop_front();

    test.charge_for_resource_allocations();

    // The balance has not changed for the canister that was already charged.
    assert_balance_change(&test, canister, duration_between_allocation_charges);
    // The balance has changed for the canisters with paused execution/install code.
    assert_balance_change(&test, paused_canister, duration_plus_one_second);
    assert_balance_change(&test, paused_install_canister, duration_plus_one_second);
}
```
