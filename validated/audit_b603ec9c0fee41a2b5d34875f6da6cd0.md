### Title
Ingress Induction Cycles Debit Silently Waived During DTS Cleanup Callback Execution - (`rs/execution_environment/src/execution/response.rs`)

### Summary

During Deterministic Time Slicing (DTS) cleanup callback execution, when a canister's balance is insufficient to cover both the pending `ingress_induction_cycles_debit` and the cycles removed during cleanup, the execution environment silently waives the ingress induction charges. The code explicitly acknowledges this: *"This allows the cleanup callback to always succeed at the expense of some ingress messages being inducted for free in this edge case."* This is a direct analog to the LiFi "free swap" bug: an existing balance condition causes a charge to be skipped, allowing resource consumption without full payment.

### Finding Description

In `handle_wasm_execution_of_cleanup_callback`, the following logic runs:

```rust
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
    .apply_ingress_induction_cycles_debit(...);
``` [1](#0-0) 

When `balance < ingress_induction_cycles_debit + removed_cycles`, the code removes `ingress_induction_cycles_debit - removed_cycles` from the pending debit, reducing it to `removed_cycles`. After `apply_ingress_induction_cycles_debit` runs, the net effect is that the canister only pays `removed_cycles` instead of the full `ingress_induction_cycles_debit`. The difference — the actual ingress induction cost for messages already inducted — is permanently forgiven.

The `ingress_induction_cycles_debit` accumulates during DTS paused execution via `add_postponed_charge_to_ingress_induction_cycles_debit`, which is called in `charge_ingress_induction_cost` when a canister has a paused execution:

```rust
if canister.has_paused_execution_or_install_code() {
    ...
    canister
        .system_state
        .add_postponed_charge_to_ingress_induction_cycles_debit(cycles);
    Ok(())
}
``` [2](#0-1) 

A secondary path exists in `apply_ingress_induction_cycles_debit` itself: if `ingress_induction_cycles_debit > cycles_balance` (a "bug" path), the remaining debit is silently dropped:

```rust
// Continue the execution by dropping the remaining debit, which makes
// some of the postponed charges free.
``` [3](#0-2) 

### Impact Explanation

An unprivileged ingress sender can have their ingress messages inducted into a canister without the canister paying the full induction cost. During DTS, while a canister's execution is paused, new ingress messages are accepted and their costs are deferred into `ingress_induction_cycles_debit`. If the canister's response callback subsequently traps (triggering cleanup) and the balance is low, those deferred charges are partially or fully waived. The canister's cycles balance is higher than it should be — the subnet effectively subsidizes the attacker's ingress message costs. This is a **cycles/resource accounting bug**: the canister's balance acts as the "latent balance" that allows free resource consumption, directly mirroring the LiFi pattern.

### Likelihood Explanation

The conditions required are: (1) a canister is in DTS with paused execution, (2) new ingress messages arrive during the pause (normal operation), (3) the response callback traps triggering cleanup, and (4) the canister's balance is low enough to trigger the waiver condition. Conditions (1)–(3) occur in normal operation for any long-running canister. Condition (4) is more specific but can be engineered by an attacker who monitors canister balances. The code itself acknowledges this as a reachable edge case, not a theoretical one.

### Recommendation

Rather than silently dropping the ingress induction debit, the system should either:
1. Track the waived amount and apply it when the canister's balance recovers (e.g., after a top-up), or
2. Reject new ingress messages to a canister whose `debited_balance()` is insufficient to cover the induction cost, even during DTS pauses, so the debit never accumulates beyond what the balance can cover.

The current approach in `remove_charge_from_ingress_induction_cycles_debit` uses saturating subtraction with no audit trail: [4](#0-3) 

### Proof of Concept

1. Deploy a canister `C` with a balance just above its freeze threshold and a long-running update method (triggering DTS).
2. Send the update call to `C`; execution pauses mid-slice.
3. While `C` is paused, send `N` ingress messages to `C`. Each is accepted and its cost is added to `ingress_induction_cycles_debit` via `add_postponed_charge_to_ingress_induction_cycles_debit`.
4. Arrange for `C`'s response callback to trap (e.g., by having the callee return an error). The cleanup callback begins.
5. During cleanup, `C` burns cycles (e.g., via stable memory growth). Now `balance < ingress_induction_cycles_debit + removed_cycles`.
6. The condition at line 490 triggers: `ingress_induction_cycles_debit - removed_cycles` is removed from the debit.
7. `apply_ingress_induction_cycles_debit` charges only `removed_cycles` from the balance.
8. The `N` ingress messages from step 3 were inducted without `C` paying their full induction cost — they were processed for free. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/execution_environment/src/execution/response.rs (L475-496)
```rust
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

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1003-1005)
```rust
    pub fn remove_charge_from_ingress_induction_cycles_debit(&mut self, charge: Cycles) {
        self.ingress_induction_cycles_debit -= charge;
    }
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1021-1038)
```rust
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
