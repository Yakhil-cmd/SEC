### Title
`liquid_cycles_balance` Does Not Account for Pending `ingress_induction_cycles_debit` During DTS Execution - (`rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs`)

---

### Summary

During Deterministic Time Slicing (DTS) multi-round execution, `SandboxSafeSystemState` is initialized with `system_state.balance()` as `initial_cycles_balance`, not `system_state.debited_balance()`. As a result, `liquid_cycles_balance()` and the underlying `withdraw_cycles_for_transfer()` do not subtract the pending `ingress_induction_cycles_debit` when computing how many cycles a canister may spend. A canister can therefore transfer up to `ingress_induction_cycles_debit` more cycles than it actually has available, ending up below the freeze threshold once the debit is applied after execution completes.

---

### Finding Description

`SandboxSafeSystemState::new()` initializes `initial_cycles_balance` from the raw balance:

```rust
// rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs, line 836
system_state.balance(),   // ← raw balance, NOT debited_balance()
``` [1](#0-0) 

`SystemState` separately tracks a pending charge field `ingress_induction_cycles_debit` that is not yet deducted from `cycles_balance`. The correct "effective" balance is returned by `debited_balance()`:

```rust
// rs/replicated_state/src/canister_state/system_state.rs
pub fn debited_balance(&self) -> Cycles {
    self.cycles_balance - self.ingress_induction_cycles_debit
}
``` [2](#0-1) 

During DTS execution, when a new ingress message arrives while the canister is paused, `charge_ingress_induction_cost` adds to `ingress_induction_cycles_debit` (using `debited_balance()` for the guard check, so the debit is bounded by the actual balance):

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs
if canister.system_state.debited_balance() < cycles + threshold {
    return Err(...);
}
canister.system_state.add_postponed_charge_to_ingress_induction_cycles_debit(cycles);
``` [3](#0-2) 

When the next DTS slice resumes, a fresh `SandboxSafeSystemState` is built with `initial_cycles_balance = balance()` (the pre-debit value). `liquid_cycles_balance()` then computes:

```rust
let cycles = self.cycles_balance();   // = balance() + in-execution changes
let threshold = freeze_threshold_cycles(...);
cycles - threshold                    // ← ingress_induction_cycles_debit NOT subtracted
``` [4](#0-3) 

`withdraw_cycles_for_transfer()` in `SandboxSafeSystemState` uses the same `self.cycles_balance()` as the starting point for the transfer check, so the transfer is permitted up to `balance() - freeze_threshold` rather than the correct `debited_balance() - freeze_threshold`:

```rust
let mut new_balance = self.cycles_balance();   // ← does not subtract debit
let result = self.cycles_account_manager.withdraw_cycles_for_transfer(
    ..., &mut new_balance, amount, ...
);
self.update_balance_change(new_balance);
``` [5](#0-4) 

After execution completes, `apply_ingress_induction_cycles_debit` deducts the full debit from the real balance:

```rust
self.consume_cycles(CompoundCycles::<IngressInduction>::new(
    self.ingress_induction_cycles_debit, cost_schedule,
));
self.ingress_induction_cycles_debit = Cycles::zero();
``` [6](#0-5) 

At that point the canister's balance is `balance() - transferred - debit`. If `transferred` was computed against the inflated `balance()` rather than `debited_balance()`, the final balance can fall below the freeze threshold.

---

### Impact Explanation

A canister executing a long DTS update call can transfer up to `ingress_induction_cycles_debit` more cycles than it actually owns. After the debit is applied post-execution, the canister's balance drops below its freeze threshold. This constitutes a **cycles conservation / resource accounting bug**: cycles are effectively "double-spent" — once transferred to another canister and once consumed by the debit — violating the invariant that a canister's balance must remain at or above the freeze threshold after any permitted operation.

---

### Likelihood Explanation

The scenario requires:
1. A canister executing a DTS (multi-round) update call (triggered by any ingress message to a canister with a large instruction limit).
2. A concurrent ingress message inducted to the same canister while it is paused between slices, adding to `ingress_induction_cycles_debit`.
3. The canister's Wasm code querying `ic0_canister_liquid_cycle_balance128` and transferring the reported maximum via `ic0_call_cycles_add128`.

Steps 1 and 2 are reachable by any unprivileged ingress sender. Step 3 is a normal canister pattern (e.g., "drain my liquid balance to a child canister"). The combination is realistic for any canister that attempts to maximize cycle transfers during a long-running execution.

---

### Recommendation

Initialize `initial_cycles_balance` in `SandboxSafeSystemState::new()` with `system_state.debited_balance()` instead of `system_state.balance()`:

```rust
// rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs
Self::new_internal(
    ...
    system_state.debited_balance(),  // was: system_state.balance()
    system_state.reserved_balance(),
    ...
)
```

This ensures that `liquid_cycles_balance()`, `withdraw_cycles_for_transfer()`, and `ic0_canister_liquid_cycle_balance128` all reflect the true spendable balance — i.e., the balance after the pending ingress induction debit is accounted for — consistent with how `charge_ingress_induction_cost` already guards new debits using `debited_balance()`.

---

### Proof of Concept

```
1. Canister C has: balance = 10_000_000, freeze_threshold = 5_000_000,
   ingress_induction_cycles_debit = 0.

2. C starts a DTS update call (long-running). After the first slice,
   C is paused with balance still = 10_000_000.

3. An external user sends a new ingress message to C.
   charge_ingress_induction_cost() checks debited_balance() = 10_000_000 >= cost + threshold,
   and adds cost = 2_000_000 to ingress_induction_cycles_debit.
   State: balance = 10_000_000, debit = 2_000_000, debited_balance = 8_000_000.

4. C's DTS execution resumes. SandboxSafeSystemState is built with
   initial_cycles_balance = balance() = 10_000_000  (NOT debited_balance = 8_000_000).

5. C calls ic0_canister_liquid_cycle_balance128:
   liquid_cycles_balance() = 10_000_000 - 5_000_000 = 5_000_000  (inflated by 2_000_000).

6. C calls ic0_call_cycles_add128(5_000_000) to transfer to canister D.
   withdraw_cycles_for_transfer checks: 10_000_000 - 5_000_000 >= 5_000_000 → OK.
   Balance change recorded: -5_000_000.

7. Execution completes. apply_balance_changes: balance = 10_000_000 - 5_000_000 = 5_000_000.
   apply_ingress_induction_cycles_debit: balance = 5_000_000 - 2_000_000 = 3_000_000.

8. Final balance = 3_000_000 < freeze_threshold = 5_000_000.
   Canister C is now frozen despite the transfer having been "permitted."
   Canister D received 5_000_000 cycles that C did not truly have available.
```

### Citations

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L828-837)
```rust
        Self::new_internal(
            system_state.canister_id(),
            CanisterStatusView::from_canister_status_type(system_state.status()),
            system_state.freeze_threshold,
            system_state.memory_allocation,
            system_state.wasm_memory_threshold,
            compute_allocation,
            system_state.environment_variables.clone().into(),
            system_state.balance(),
            system_state.reserved_balance(),
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L900-917)
```rust
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

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L1081-1106)
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
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L945-950)
```rust
    /// Returns the balance after applying the pending 'ingress_induction_cycles_debit'.
    /// Returns 0 if the balance is smaller than the pending 'ingress_induction_cycles_debit'.
    pub fn debited_balance(&self) -> Cycles {
        // We rely on saturating operations of `Cycles` here.
        self.cycles_balance - self.ingress_induction_cycles_debit
    }
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1034-1038)
```rust
        self.consume_cycles(CompoundCycles::<IngressInduction>::new(
            self.ingress_induction_cycles_debit,
            cost_schedule,
        ));
        self.ingress_induction_cycles_debit = Cycles::zero();
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
