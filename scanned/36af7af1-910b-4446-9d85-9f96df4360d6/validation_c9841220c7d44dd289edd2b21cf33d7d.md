### Title
Silent Ingress-Induction Cycles Forgiveness During DTS Cleanup-Callback Execution — (`rs/execution_environment/src/execution/response.rs`)

---

### Summary

The IC execution environment contains a deliberate silent-error-continuation pattern in `handle_wasm_execution_of_cleanup_callback`. When a canister's balance is insufficient to cover both its pending `ingress_induction_cycles_debit` and cycles removed during a cleanup callback, the pending debit is silently reduced before being applied. This causes previously accepted ingress messages to be inducted without the canister paying their full cost — a direct analog to the Compound Finance "error propagation" pattern where errors are swallowed rather than propagated.

A second, structurally identical pattern exists in `apply_ingress_induction_cycles_debit` in `system_state.rs`, where an overflow of debit over balance is logged but execution continues by forgiving the excess debit. A third instance exists in `ConsumedCyclesForInstructions::apply` in `execution_environment.rs`, where a failed cycles charge on a management-operation failure path is only logged and silently ignored.

---

### Finding Description

**Pattern 1 — Cleanup callback path (primary, reachable):**

In `handle_wasm_execution_of_cleanup_callback`:

```rust
// rs/execution_environment/src/execution/response.rs ~L490-496
if self.canister.system_state.balance() < ingress_induction_cycles_debit + removed_cycles {
    self.canister
        .system_state
        .remove_charge_from_ingress_induction_cycles_debit(
            ingress_induction_cycles_debit - removed_cycles,
        );
}
self.canister
    .system_state
    .apply_ingress_induction_cycles_debit(...);
```

When `balance < debit + removed_cycles`, the debit is silently reduced from `ingress_induction_cycles_debit` to `removed_cycles`. The code comment explicitly acknowledges this: *"This allows the cleanup callback to always succeed at the expense of some ingress messages being inducted for free in this edge case."*

The forgiven amount is `ingress_induction_cycles_debit − removed_cycles` cycles — real cycles that were owed for ingress induction but are silently written off. [1](#0-0) 

**Pattern 2 — `apply_ingress_induction_cycles_debit` (secondary, labeled unreachable):**

```rust
// rs/replicated_state/src/canister_state/system_state.rs ~L1021-1033
if remaining_debit.get() > 0 {
    // This case is unreachable and may happen only due to a bug
    charging_from_balance_error.inc();
    error!(log, "[EXC-BUG]: Debited cycles exceed the cycles balance...");
    // Continue the execution by dropping the remaining debit, which makes
    // some of the postponed charges free.
}
self.consume_cycles(CompoundCycles::<IngressInduction>::new(
    self.ingress_induction_cycles_debit,
    cost_schedule,
));
```

When `ingress_induction_cycles_debit > cycles_balance`, the excess debit is silently dropped. The `consume_cycles` call uses saturating arithmetic, so the balance drains to zero and the remaining debit is forgiven. [2](#0-1) 

**Pattern 3 — `ConsumedCyclesForInstructions::apply` (tertiary, management-op failure path):**

```rust
// rs/execution_environment/src/execution_environment.rs ~L351-368
let res = self.cycles_account_manager.consume_cycles(
    &mut canister.system_state, ..., self.consumed_cycles, ...,
    true, /* we only log the error, but do not return it to the user */
);
if let Err(err) = res {
    failed_charge.inc();
    error!(self.log, "[EXC-BUG]: Failed to charge {:?} cycles...", ...);
}
```

When a management operation (e.g., `upload_chunk`, `read_canister_snapshot_data`) fails and the canister state is rolled back, `apply()` attempts to re-charge cycles consumed before the failure. If this charge fails, the error is only logged — the canister receives free execution of the partial management operation. [3](#0-2) 

---

### Impact Explanation

**Cycles conservation violation (Pattern 1):** An unprivileged canister can arrange for ingress messages to be inducted without paying their full induction cost. The forgiven amount equals `ingress_induction_cycles_debit − removed_cycles`. Since `ingress_induction_cycles_debit` accumulates across multiple concurrent ingress messages during a DTS-paused execution, the forgiven amount can be non-trivial.

**Cycles conservation violation (Pattern 2):** If the "unreachable" branch in `apply_ingress_induction_cycles_debit` is triggered (e.g., via the cleanup-callback path or a future code change), the excess debit is silently forgiven, violating the invariant that `ingress_induction_cycles_debit ≤ cycles_balance`.

**Free management-op execution (Pattern 3):** A canister that is out of cycles but passes initial checks (due to a race between state rollback and re-charge) could execute partial management operations without paying cycles.

All three patterns share the root cause identified in the external report: errors are not trapped when found; instead, execution continues with a silent best-effort outcome, violating the "fail early and loudly" principle.

---

### Likelihood Explanation

**Pattern 1** is **Medium** likelihood. It requires:
1. A canister with a DTS-paused execution (common for long-running `install_code` or update calls).
2. Concurrent ingress messages sent to the paused canister (normal user behavior).
3. The response callback failing and triggering a cleanup callback.
4. The cleanup callback removing cycles (e.g., via `ic0.call_cycles_add128`).

Steps 1–3 are routine in production. Step 4 requires a canister specifically designed to remove cycles in its cleanup callback, which is a valid and common pattern for releasing resources.

**Pattern 2** is **Low** likelihood as it is labeled unreachable, but the cleanup-callback path (Pattern 1) demonstrates that the precondition (`debit > balance`) can be approached in practice.

**Pattern 3** is **Low** likelihood as the rollback restores the exact state that allowed the initial charge, making the re-charge failure unlikely under normal conditions.

---

### Recommendation

1. **Pattern 1**: Instead of silently reducing the debit, the cleanup callback should be allowed to fail if cycles are insufficient, or the ingress induction cost should be charged before the cleanup callback begins (not deferred). If the design intent is to always run cleanup callbacks, the forgiven amount should be explicitly bounded and documented as a known invariant violation with a hard cap.

2. **Pattern 2**: Replace the silent continuation with a `debug_assert` that panics in test/debug builds and a hard cap in production that prevents the balance from going below zero, rather than silently forgiving debt.

3. **Pattern 3**: If the charge in `apply()` fails, the management operation result should be converted to an error rather than silently succeeding. The comment "we only log the error, but do not return it to the user" is the exact anti-pattern described in the external report.

4. **General**: Adopt the "fail early and loudly" principle for cycles accounting. Cycles errors should propagate as `Result::Err` and be handled by callers, not silently swallowed with a log message.

---

### Proof of Concept

**Attack path for Pattern 1:**

1. Deploy a canister `A` with a response callback that always fails (traps) and a cleanup callback that calls `ic0.call_cycles_add128` to send cycles to another canister `B`.

2. Send a long-running update call to `A` that triggers DTS (paused execution).

3. While `A` is paused, send multiple ingress messages to `A`. These are accepted (the induction cost is checked against `debited_balance()`) and their cost is accumulated in `ingress_induction_cycles_debit`.

4. When `A`'s execution resumes and the response callback fails, the cleanup callback runs. It removes cycles (sends them to `B`).

5. If `A.balance < ingress_induction_cycles_debit + removed_cycles`, the condition at line 490 of `response.rs` triggers, reducing the debit by `ingress_induction_cycles_debit - removed_cycles`.

6. `apply_ingress_induction_cycles_debit` is called with the reduced debit. The ingress messages are fully inducted but only `removed_cycles` worth of induction cost is charged.

7. The forgiven amount (`ingress_induction_cycles_debit - removed_cycles`) represents real cycles that were owed but never paid. [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/execution_environment/src/execution/response.rs (L463-504)
```rust
    /// Processes the output and the state changes of Wasm execution of the
    /// cleanup callback.
    fn handle_wasm_execution_of_cleanup_callback(
        mut self,
        mut output: WasmExecutionOutput,
        canister_state_changes: CanisterStateChanges,
        callback_err: HypervisorError,
        original: &OriginalContext,
        round: &RoundContext,
        round_limits: &mut RoundLimits,
        call_tree_metrics: &dyn CallTreeMetrics,
    ) -> ExecuteMessageResult {
        // The ingress induction debit can interfere with cycles changes that happened concurrently
        // during the cleanup callback execution. If the balance of the canister is not enough to
        // cover the debit + the amount of removed cycles during execution, the canister might end
        // up with an incorrect balance. To avoid this, we check if the balance is enough to cover
        // the debit + the removed cycles to ensure that the cycles change can be performed.
        //
        // This allows the cleanup callback to always succeed at the expense of some ingress
        // messages being inducted for free in this edge case. This is acceptable because the cleanup
        // callback is expected to always run and allow the canister to perform important cleanup tasks,
        // like releasing locks or undoing other state changes.
        let ingress_induction_cycles_debit =
            self.canister.system_state.ingress_induction_cycles_debit();
        let removed_cycles = canister_state_changes
            .system_state_modifications
            .removed_cycles();
        if self.canister.system_state.balance() < ingress_induction_cycles_debit + removed_cycles {
            self.canister
                .system_state
                .remove_charge_from_ingress_induction_cycles_debit(
                    ingress_induction_cycles_debit - removed_cycles,
                );
        }
        self.canister
            .system_state
            .apply_ingress_induction_cycles_debit(
                self.canister.canister_id(),
                round.cost_schedule,
                round.log,
                round.counters.charging_from_balance_error,
            );
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L983-1039)
```rust
    /// Records the given amount as debit that will be charged from the balance
    /// at some point in the future.
    ///
    /// Precondition:
    /// - `charge <= self.debited_balance()`.
    pub fn add_postponed_charge_to_ingress_induction_cycles_debit(&mut self, charge: Cycles) {
        assert!(
            charge <= self.debited_balance(),
            "Insufficient cycles for a postponed charge: {} vs {}",
            charge,
            self.debited_balance()
        );
        self.ingress_induction_cycles_debit += charge;
    }

    /// Removes a previously postponed charge for ingress messages from the balance
    /// of the canister.
    ///
    /// Note that this will saturate the balance to zero if the charge to remove is
    /// larger than the current debit.
    pub fn remove_charge_from_ingress_induction_cycles_debit(&mut self, charge: Cycles) {
        self.ingress_induction_cycles_debit -= charge;
    }

    /// Charges the pending 'ingress_induction_cycles_debit' from the balance.
    ///
    /// Precondition:
    /// - The balance is large enough to cover the debit.
    pub fn apply_ingress_induction_cycles_debit(
        &mut self,
        canister_id: CanisterId,
        cost_schedule: CanisterCyclesCostSchedule,
        log: &ReplicaLogger,
        charging_from_balance_error: &IntCounter,
    ) {
        // We rely on saturating operations of `Cycles` here.
        let remaining_debit = self.ingress_induction_cycles_debit - self.cycles_balance;
        debug_assert_eq!(remaining_debit.get(), 0);
        if remaining_debit.get() > 0 {
            // This case is unreachable and may happen only due to a bug: if the
            // caller has reduced the cycles balance below the cycles debit.
            charging_from_balance_error.inc();
            error!(
                log,
                "[EXC-BUG]: Debited cycles exceed the cycles balance of {} by {} in install_code",
                canister_id,
                remaining_debit,
            );
            // Continue the execution by dropping the remaining debit, which makes
            // some of the postponed charges free.
        }
        self.consume_cycles(CompoundCycles::<IngressInduction>::new(
            self.ingress_induction_cycles_debit,
            cost_schedule,
        ));
        self.ingress_induction_cycles_debit = Cycles::zero();
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L309-371)
```rust
/// Accumulates instructions used and cycles consumed for those instructions while executing
/// a management operation so that, if the operation fails, the cycles consumed before the
/// failure can be charged and the instructions can be accounted for.
pub(crate) struct ConsumedCyclesForInstructions<'a> {
    consumed_cycles: CompoundCycles<Instructions>,
    instructions_used: NumInstructions,
    cycles_account_manager: &'a CyclesAccountManager,
    log: &'a ReplicaLogger,
}

impl<'a> ConsumedCyclesForInstructions<'a> {
    fn new(
        cycles_account_manager: &'a CyclesAccountManager,
        cost_schedule: CanisterCyclesCostSchedule,
        log: &'a ReplicaLogger,
    ) -> Self {
        Self {
            consumed_cycles: CompoundCycles::new(Cycles::zero(), cost_schedule),
            instructions_used: NumInstructions::new(0),
            cycles_account_manager,
            log,
        }
    }

    pub(crate) fn add(
        &mut self,
        cycles: CompoundCycles<Instructions>,
        instructions: NumInstructions,
    ) {
        self.consumed_cycles += cycles;
        self.instructions_used += instructions;
    }

    pub(crate) fn apply(
        self,
        canister: &mut CanisterState,
        round_limits: &mut RoundLimits,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
        failed_charge: &IntCounter,
    ) {
        let memory_usage = canister.memory_usage();
        let message_memory_usage = canister.message_memory_usage();
        let res = self.cycles_account_manager.consume_cycles(
            &mut canister.system_state,
            memory_usage,
            message_memory_usage,
            self.consumed_cycles,
            subnet_cycles_config,
            true, /* we only log the error, but do not return it to the user => do reveal top up balance */
        );
        if let Err(err) = res {
            failed_charge.inc();
            error!(
                self.log,
                "[EXC-BUG]: Failed to charge {:?} cycles on canister {}: {}",
                self.consumed_cycles,
                canister.canister_id(),
                err
            );
        }
        round_limits.instructions -= as_round_instructions(self.instructions_used);
    }
}
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L316-357)
```rust
    pub fn charge_ingress_induction_cost(
        &self,
        canister: &mut CanisterState,
        canister_current_memory_usage: NumBytes,
        canister_current_message_memory_usage: MessageMemoryUsage,
        canister_compute_allocation: ComputeAllocation,
        cycles: Cycles,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
        reveal_top_up: bool,
    ) -> Result<(), CanisterOutOfCyclesError> {
        let threshold = self.freeze_threshold_cycles(
            canister.system_state.freeze_threshold,
            canister.system_state.memory_allocation,
            canister_current_memory_usage,
            canister_current_message_memory_usage,
            canister_compute_allocation,
            subnet_cycles_config,
            canister.system_state.reserved_balance(),
        );
        if canister.has_paused_execution_or_install_code() {
            if canister.system_state.debited_balance() < cycles + threshold {
                return Err(CanisterOutOfCyclesError {
                    canister_id: canister.canister_id(),
                    available: canister.system_state.debited_balance(),
                    requested: cycles,
                    threshold,
                    reveal_top_up,
                });
            }
            canister
                .system_state
                .add_postponed_charge_to_ingress_induction_cycles_debit(cycles);
            Ok(())
        } else {
            self.consume_with_threshold::<IngressInduction>(
                &mut canister.system_state,
                CompoundCycles::new(cycles, subnet_cycles_config.cost_schedule),
                threshold,
                reveal_top_up,
            )
        }
    }
```
