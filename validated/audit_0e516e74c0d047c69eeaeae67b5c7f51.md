Audit Report

## Title
`ingress_induction_cycles_debit` Not Zeroed When `removed_cycles` Exceeds Debit in DTS Cleanup Callback - (File: rs/execution_environment/src/execution/response.rs)

## Summary
In `handle_wasm_execution_of_cleanup_callback`, when a canister's balance cannot cover both `ingress_induction_cycles_debit` and `removed_cycles`, the code computes a debit reduction as `ingress_induction_cycles_debit - removed_cycles`. Because `Cycles` implements fully saturating arithmetic, this expression saturates to `Cycles::zero()` when `removed_cycles > ingress_induction_cycles_debit`, making the subsequent `remove_charge_from_ingress_induction_cycles_debit(0)` call a no-op. The debit is left at its original value instead of being zeroed, causing the canister to be overcharged by up to `ingress_induction_cycles_debit` cycles and, in the sub-case where `balance < debit`, triggering the `[EXC-BUG]` invariant-violation error path.

## Finding Description

**Root cause — saturating subtraction produces zero:**

`Cycles::sub` is implemented with `saturating_sub` throughout the IC codebase:

```rust
// rs/types/cycles/src/cycles.rs L133-138
impl Sub for Cycles {
    type Output = Self;
    fn sub(self, rhs: Self) -> Self {
        Self(self.0.saturating_sub(rhs.0))   // saturates to 0 on underflow
    }
}
``` [1](#0-0) 

**Buggy code path:**

```rust
// rs/execution_environment/src/execution/response.rs L485-496
let ingress_induction_cycles_debit =
    self.canister.system_state.ingress_induction_cycles_debit();
let removed_cycles = canister_state_changes
    .system_state_modifications
    .removed_cycles();
if self.canister.system_state.balance() < ingress_induction_cycles_debit + removed_cycles {
    self.canister
        .system_state
        .remove_charge_from_ingress_induction_cycles_debit(
            ingress_induction_cycles_debit - removed_cycles,  // saturates to 0 when R > D
        );
}
``` [2](#0-1) 

When `removed_cycles (R) > ingress_induction_cycles_debit (D)`, the expression `D - R` saturates to `Cycles::zero()`. The call `remove_charge_from_ingress_induction_cycles_debit(0)` executes `self.ingress_induction_cycles_debit -= Cycles::zero()`, which is a no-op: [3](#0-2) 

The debit remains at `D` instead of being zeroed.

**Downstream effect in `apply_ingress_induction_cycles_debit`:**

```rust
// rs/replicated_state/src/canister_state/system_state.rs L1018-1038
let remaining_debit = self.ingress_induction_cycles_debit - self.cycles_balance;
debug_assert_eq!(remaining_debit.get(), 0);
if remaining_debit.get() > 0 {
    charging_from_balance_error.inc();
    error!(log, "[EXC-BUG]: Debited cycles exceed the cycles balance ...");
}
self.consume_cycles(CompoundCycles::<IngressInduction>::new(
    self.ingress_induction_cycles_debit,
    cost_schedule,
));
self.ingress_induction_cycles_debit = Cycles::zero();
``` [4](#0-3) 

Because the debit was not reduced, `consume_cycles` charges the full `D` from the balance. Then `apply_canister_state_changes` additionally deducts `R`. The canister ends up with `max(0, B - D - R)` instead of the correct `max(0, B - R)`, losing an extra `min(D, B - R)` cycles. When `B < D` (balance already below debit), `remaining_debit > 0` and the `[EXC-BUG]` counter is incremented.

**Why existing guards are insufficient:**

The guard `if balance < D + R` correctly identifies the problematic case but the fix inside it is arithmetically wrong for the sub-case `R > D`. The `add_postponed_charge_to_ingress_induction_cycles_debit` assertion (`charge <= debited_balance`) only prevents the debit from exceeding the balance at induction time; it does not prevent the balance from later falling below the debit due to `removed_cycles` applied by the cleanup callback. [5](#0-4) 

## Impact Explanation

A canister undergoing DTS execution loses up to `ingress_induction_cycles_debit` extra cycles that the protocol explicitly intended to waive. The extra loss equals `min(D, max(0, B - R))`, bounded above by `D`. When `B < D` after the no-op, the `[EXC-BUG]` invariant-violation counter is incremented and the protocol continues with incorrect state. This constitutes a moderate user-funds impact: the canister is incorrectly overcharged for ingress induction in an edge case the code was designed to handle for free, matching the **Medium ($200–$2,000) — moderate user-funds/security impact** bounty tier.

## Likelihood Explanation

All four required conditions are reachable by an unprivileged canister owner without any special privileges:
1. DTS is active on IC mainnet — any sufficiently complex canister call can trigger multi-round execution.
2. Ingress messages can be sent to a paused canister by any external caller, accumulating `ingress_induction_cycles_debit`.
3. A response callback can fail (e.g., trap), triggering the cleanup path.
4. A cleanup callback can send cycles to another canister via an inter-canister call, producing `removed_cycles > D`.

The scenario is repeatable and does not require node control, governance majority, or any privileged access.

## Recommendation

Replace the saturating subtraction with an explicit branch that zeroes the debit entirely when `removed_cycles >= ingress_induction_cycles_debit`:

```rust
if self.canister.system_state.balance() < ingress_induction_cycles_debit + removed_cycles {
    let reduction = if removed_cycles >= ingress_induction_cycles_debit {
        ingress_induction_cycles_debit  // zero out the debit entirely
    } else {
        ingress_induction_cycles_debit - removed_cycles
    };
    self.canister
        .system_state
        .remove_charge_from_ingress_induction_cycles_debit(reduction);
}
```

This ensures the debit is always reduced to zero when `R >= D`, matching the stated intent that "the cleanup callback can always succeed."

## Proof of Concept

**Concrete unit test scenario** (values chosen to satisfy all conditions):

- `B = 100`, `D = 50` (`ingress_induction_cycles_debit`), `R = 80` (`removed_cycles`)
- Precondition check: `D <= B` ✓ (satisfied by `add_postponed_charge_to_ingress_induction_cycles_debit` assertion)
- Bug trigger: `R > D` ✓ and `B < D + R = 130` ✓

**Buggy execution trace:**
1. `ingress_induction_cycles_debit - removed_cycles = 50 - 80 = 0` (saturated)
2. `remove_charge_from_ingress_induction_cycles_debit(0)` → debit stays at 50
3. `apply_ingress_induction_cycles_debit`: `remaining_debit = 50 - 100 = 0` (saturated, no error here); consumes 50 → balance = 50
4. `apply_canister_state_changes`: consumes 80 → balance saturates to 0
5. **Result:** balance = 0 (should be 20); canister loses 20 extra cycles

**Correct execution trace:**
1. Reduction = 50 (zero out debit)
2. `apply_ingress_induction_cycles_debit`: consumes 0 → balance = 100
3. `apply_canister_state_changes`: consumes 80 → balance = 20
4. **Result:** balance = 20, no error

A deterministic unit test can be written against `handle_wasm_execution_of_cleanup_callback` by constructing a `CanisterState` with the above parameters, a failing response callback, and a cleanup callback that removes 80 cycles, then asserting the final balance equals 20.

### Citations

**File:** rs/types/cycles/src/cycles.rs (L133-138)
```rust
impl Sub for Cycles {
    type Output = Self;

    fn sub(self, rhs: Self) -> Self {
        Self(self.0.saturating_sub(rhs.0))
    }
```

**File:** rs/execution_environment/src/execution/response.rs (L485-496)
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
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L988-995)
```rust
    pub fn add_postponed_charge_to_ingress_induction_cycles_debit(&mut self, charge: Cycles) {
        assert!(
            charge <= self.debited_balance(),
            "Insufficient cycles for a postponed charge: {} vs {}",
            charge,
            self.debited_balance()
        );
        self.ingress_induction_cycles_debit += charge;
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1003-1005)
```rust
    pub fn remove_charge_from_ingress_induction_cycles_debit(&mut self, charge: Cycles) {
        self.ingress_induction_cycles_debit -= charge;
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
