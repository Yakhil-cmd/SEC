### Title
Cycles Debit Forgiven When Balance Drops Below `ingress_induction_cycles_debit` During DTS Execution — (`rs/replicated_state/src/canister_state/system_state.rs`)

---

### Summary

During Deterministic Time Slicing (DTS), ingress induction costs for messages arriving while a canister is paused are recorded as a postponed charge (`ingress_induction_cycles_debit`). The check that validates this charge uses `debited_balance()` (balance minus pending debit). However, cycles operations during resumed execution — `ic0.call_cycles_add128` and `ic0.cycles_burn128` — use the raw `balance()` (not `debited_balance()`), allowing the canister to reduce its balance below the pending debit. When `apply_ingress_induction_cycles_debit` is called at the end of execution, the code explicitly acknowledges and silently forgives the excess debit, violating cycles conservation.

---

### Finding Description

**Phase 1 — Debit is added using `debited_balance()` as the guard:**

When a canister has a paused execution and a new ingress message arrives, `charge_ingress_induction_cost` validates the charge against `debited_balance()` (= `cycles_balance - ingress_induction_cycles_debit`) and then appends to the debit: [1](#0-0) 

The precondition in `add_postponed_charge_to_ingress_induction_cycles_debit` asserts `charge <= debited_balance()`: [2](#0-1) 

At this point the invariant holds: `cycles_balance >= ingress_induction_cycles_debit`.

**Phase 2 — Execution resumes; cycles operations use raw `balance()`, not `debited_balance()`:**

When the canister resumes, `SandboxSafeSystemState.withdraw_cycles_for_transfer` passes `self.cycles_balance()` — derived from `initial_cycles_balance = system_state.balance()` (raw, not debited) — to `CyclesAccountManager.withdraw_cycles_for_transfer`: [3](#0-2) 

`withdraw_cycles_for_transfer` checks only against the freeze threshold, not against `ingress_induction_cycles_debit`: [4](#0-3) 

Similarly, `cycles_burn128` is clamped to `liquid_cycles_balance = balance - freeze_threshold`, again ignoring the pending debit: [5](#0-4) 

A canister can therefore reduce its balance to `freeze_threshold` (potentially 0) during execution, making `cycles_balance < ingress_induction_cycles_debit`.

**Phase 3 — Debit application silently forgives the excess:**

At the end of execution, `apply_ingress_induction_cycles_debit` is called. The code detects the invariant violation but explicitly continues, forgiving the remaining debit: [6](#0-5) 

The `debug_assert_eq!(remaining_debit.get(), 0)` fires only in debug builds. In production, the error is logged and the excess debit is dropped. `consume_cycles` then saturates the balance to zero, and the forgiven portion of the debit is never charged.

---

### Impact Explanation

A canister developer can craft a canister that avoids paying the full ingress induction cost for messages received while it is paused under DTS. The forgiven cycles represent a direct violation of cycles conservation: the subnet accepts and queues ingress messages whose induction cost is never fully collected. At scale (many paused canisters, many concurrent ingress messages), this could be used to drain subnet resources without proportional payment.

---

### Likelihood Explanation

The attack requires: (1) a canister running a long DTS update, (2) the attacker sending ingress messages to it while it is paused to accumulate `ingress_induction_cycles_debit`, and (3) the canister's Wasm code calling `ic0.call_cycles_add128` or `ic0.cycles_burn128` to reduce the balance below the debit before the slice completes. All three conditions are fully under the control of an unprivileged canister developer. The `debug_assert` confirms the developers consider this path unreachable, but the code path is reachable in production.

---

### Recommendation

`withdraw_cycles_for_transfer` and `cycles_burn128` should account for the pending `ingress_induction_cycles_debit` when computing the available balance. Concretely, the effective spendable balance during execution should be `debited_balance()` (i.e., `cycles_balance - ingress_induction_cycles_debit`), not the raw `cycles_balance`. Alternatively, `apply_ingress_induction_cycles_debit` should treat a shortfall as a hard error rather than silently forgiving it.

---

### Proof of Concept

1. Deploy canister A with freeze threshold = 0 and initial balance = 1,000,000 cycles.
2. Trigger a long-running update on canister A (e.g., a loop that exceeds one DTS slice).
3. While canister A is paused between slices, send 5 ingress messages to it. Each message costs ~100,000 cycles in induction cost. `charge_ingress_induction_cost` validates each against `debited_balance()` and appends to `ingress_induction_cycles_debit` (total = 500,000 cycles). The assert in `add_postponed_charge_to_ingress_induction_cycles_debit` passes because `debited_balance() = 1,000,000 - 500,000 = 500,000 >= 0`.
4. Canister A's Wasm code calls `ic0.call_cycles_add128(900,000)` to transfer 900,000 cycles to canister B (also controlled by the attacker). `withdraw_cycles_for_transfer` checks `balance() = 1,000,000 >= freeze_threshold(0) + 900,000` — passes. Balance becomes 100,000.
5. Execution ends. `apply_ingress_induction_cycles_debit` is called: `remaining_debit = 500,000 - 100,000 = 400,000 > 0`. The error is logged, `consume_cycles(500,000)` saturates balance to 0, and 400,000 cycles of debit are forgiven.
6. Net result: attacker moved 900,000 cycles to canister B and paid only 100,000 cycles for ingress induction instead of 500,000 — a gain of 400,000 cycles at the subnet's expense.

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L276-305)
```rust
    pub fn withdraw_cycles_for_transfer(
        &self,
        canister_id: CanisterId,
        freeze_threshold: NumSeconds,
        memory_allocation: MemoryAllocation,
        canister_current_memory_usage: NumBytes,
        canister_current_message_memory_usage: MessageMemoryUsage,
        canister_compute_allocation: ComputeAllocation,
        cycles_balance: &mut Cycles,
        cycles: Cycles,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
        reserved_balance: Cycles,
        reveal_top_up: bool,
    ) -> Result<(), CanisterOutOfCyclesError> {
        self.withdraw_with_threshold(
            canister_id,
            cycles_balance,
            cycles,
            self.freeze_threshold_cycles(
                freeze_threshold,
                memory_allocation,
                canister_current_memory_usage,
                canister_current_message_memory_usage,
                canister_compute_allocation,
                subnet_cycles_config,
                reserved_balance,
            ),
            reveal_top_up,
        )
    }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L335-348)
```rust
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
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L986-996)
```rust
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
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1018-1038)
```rust
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
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L898-917)
```rust
    /// Computes the current liquid main balance of the canister that the canister can spend
    /// without getting frozen based on the initial value and the changes during the execution.
    pub(super) fn liquid_cycles_balance(
        &self,
        current_memory_usage: NumBytes,
        current_message_memory_usage: MessageMemoryUsage,
    ) -> Cycles {
        let cycles = self.cycles_balance();
        let threshold = self.cycles_account_manager.freeze_threshold_cycles(
            self.freeze_threshold,
            self.memory_allocation,
            current_memory_usage,
            current_message_memory_usage,
            self.compute_allocation,
            self.subnet_cycles_config,
            self.reserved_balance(),
        );
        // Here we rely on the saturating subtraction for Cycles.
        cycles - threshold
    }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L1081-1107)
```rust
    pub(super) fn withdraw_cycles_for_transfer(
        &mut self,
        canister_current_memory_usage: NumBytes,
        canister_current_message_memory_usage: MessageMemoryUsage,
        amount: Cycles,
        reveal_top_up: bool,
    ) -> HypervisorResult<()> {
        let mut new_balance = self.cycles_balance();
        let result = self
            .cycles_account_manager
            .withdraw_cycles_for_transfer(
                self.canister_id,
                self.freeze_threshold,
                self.memory_allocation,
                canister_current_memory_usage,
                canister_current_message_memory_usage,
                self.compute_allocation,
                &mut new_balance,
                amount,
                self.subnet_cycles_config,
                self.reserved_balance(),
                reveal_top_up,
            )
            .map_err(HypervisorError::InsufficientCyclesBalance);
        self.update_balance_change(new_balance);
        result
    }
```
