### Title
Panicking `.unwrap()` in `apply_balance_changes` Triggered by Compromised Sandbox — (`rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs`)

---

### Summary

`SystemStateModifications::apply_balance_changes`, called unconditionally from `apply_changes` during post-execution state application, contains a `.unwrap()` on `state.reserve_cycles(self.reserved_cycles)` (line 600) and an `assert_eq!` balance invariant check (line 608). Neither is guarded against data crafted by a compromised sandboxed process. A malicious canister that achieves arbitrary code execution inside its sandbox can craft a `SystemStateModifications` payload that passes `validate_cycle_change` but causes `reserve_cycles` to return `Err(ReservationError::ReservedLimitExceed)`, triggering the `.unwrap()` panic and crashing the replica.

---

### Finding Description

`apply_changes` (the post-execution state application function) was partially hardened: it now returns `HypervisorResult` and propagates most errors with `?`. However, it unconditionally calls `apply_balance_changes` at line 409 before any further error-returning logic: [1](#0-0) 

`apply_balance_changes` itself is not fallible — it returns `()` — and contains two panicking constructs:

**Panic 1 — `.unwrap()` on `reserve_cycles`:** [2](#0-1) 

`reserve_cycles` returns `Err(ReservationError::ReservedLimitExceed)` when `self.reserved_balance + amount > reserved_balance_limit`: [3](#0-2) 

**Panic 2 — `assert_eq!` balance invariant:** [4](#0-3) 

The only upstream guard is `validate_cycle_change`, which checks only that no cycles were created from thin air (internal accounting consistency): [5](#0-4) 

Critically, `validate_cycle_change` **does not** check whether `reserved_cycles` exceeds the canister's `reserved_balance_limit`. It only verifies that `cycles_balance_change == call_context_balance_taken - request_payments - consumed_cycles - reserved_cycles`. A compromised sandbox can set `reserved_cycles = V` and `cycles_balance_change = Removed(V + other_costs)` — this passes `validate_cycle_change` — while V is chosen to exceed `reserved_balance_limit - current_reserved_balance`, causing `reserve_cycles(V)` to return `Err(ReservationError::ReservedLimitExceed)` and the `.unwrap()` to panic.

---

### Impact Explanation

A replica panic in the execution path crashes the replica process. Because all replicas on a subnet process the same messages deterministically, every replica will hit the same panic on the same message, causing a subnet-wide crash loop. Recovery requires subnet recovery procedures involving multiple teams. This is a complete availability break for the affected subnet.

---

### Likelihood Explanation

Exploiting this requires a prior sandbox escape (arbitrary code execution within the canister sandbox process). Sandbox escapes are non-trivial but are a known attack class against Wasm runtimes. The IC's threat model explicitly considers compromised sandbox processes as an adversary (the sandbox isolation layer exists precisely to contain such compromises). Once sandbox code execution is achieved, crafting the triggering `SystemStateModifications` payload is straightforward: set `reserved_cycles` to any value exceeding `reserved_balance_limit - current_reserved_balance` and adjust `cycles_balance_change` accordingly. Any canister with a `reserved_balance_limit` set (a standard configuration) is a viable target.

---

### Recommendation

- **Short term:** Change `apply_balance_changes` to return `HypervisorResult<()>` and replace `state.reserve_cycles(self.reserved_cycles).unwrap()` with `state.reserve_cycles(self.reserved_cycles).map_err(Self::error)?`. Replace the `assert_eq!` balance invariant with a `debug_assert_eq!` (or a returning error). Propagate the result up through `apply_changes`.
- **Long term:** Audit all code reachable from `apply_changes` and the broader post-sandbox-execution path for any remaining `unwrap`, `expect`, `assert`, or `unreachable` calls that operate on fields sourced from `SystemStateModifications` or any other sandbox-supplied data structure.

---

### Proof of Concept

A compromised sandbox process crafts and returns the following `SystemStateModifications`:

```
reserved_cycles          = reserved_balance_limit - current_reserved_balance + 1
cycles_balance_change    = Removed(reserved_cycles)   // passes validate_cycle_change
consumed_cycles_by_use_case = default (zero)
call_context_balance_taken  = None
requests                 = []
```

Execution path on the replica:

1. `apply_changes` calls `validate_cycle_change` → passes (accounting is internally consistent: `cycles_balance_change == Removed(reserved_cycles)`)
2. `apply_changes` calls `apply_balance_changes`
3. `apply_balance_changes` computes `adjusted_balance_change = Removed(reserved_cycles) + Added(reserved_cycles) = zero`, applies it (no net balance change yet)
4. `state.reserve_cycles(reserved_cycles)` → `can_reserve_cycles` checks `self.reserved_balance + reserved_cycles > reserved_balance_limit` → returns `Err(ReservationError::ReservedLimitExceed)`
5. `.unwrap()` panics → replica process crashes → subnet enters crash loop [6](#0-5) [7](#0-6)

### Citations

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L160-191)
```rust
    fn validate_cycle_change(&self, is_cmc_canister: bool) -> HypervisorResult<()> {
        let mut expected_change = CyclesBalanceChange::zero();

        if let Some((_, call_context_balance_taken)) = self.call_context_balance_taken {
            expected_change =
                expected_change + CyclesBalanceChange::added(call_context_balance_taken);
        }

        for req in self.requests.iter() {
            expected_change = expected_change + CyclesBalanceChange::removed(req.payment);
        }

        for amount in self.consumed_cycles_by_use_case.iter_over_real() {
            expected_change = expected_change + CyclesBalanceChange::removed(amount);
        }

        expected_change = expected_change + CyclesBalanceChange::removed(self.reserved_cycles);

        // If the canister is not the cycles minting canister, then the balance
        // change coming from the Wasm execution must match the expected balance
        // change that we just computed.
        if is_cmc_canister || self.cycles_balance_change == expected_change {
            Ok(())
        } else {
            Err(HypervisorError::WasmEngineError(
                WasmEngineError::FailedToApplySystemChanges(format!(
                    "Invalid cycle change: expected {:?}, got {:?}",
                    expected_change, self.cycles_balance_change
                )),
            ))
        }
    }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L407-409)
```rust
        // Verify total cycle change is not positive and update cycles balance.
        self.validate_cycle_change(system_state.canister_id() == CYCLES_MINTING_CANISTER_ID)?;
        self.apply_balance_changes(system_state);
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L555-609)
```rust
    pub fn apply_balance_changes(&self, state: &mut SystemState) {
        let initial_balance = state.balance();

        // `self.cycles_balance_change` consists of:
        // - CyclesBalanceChange::added(cycles_accepted_from_the_call_context)
        // - CyclesBalanceChange::remove(cycles_sent_via_outgoing_calls)
        // - CyclesBalanceChange::remove(cycles_consumed_by_various_fees)
        // - CyclesBalanceChange::remove(reserved_cycles)
        //
        // The latter two cases are applied with higher-level helpers, so we
        // need to compute the balance change with those cases excluded.
        let mut adjusted_balance_change = self.cycles_balance_change;
        for amount in self.consumed_cycles_by_use_case.iter_over_real() {
            adjusted_balance_change = adjusted_balance_change + CyclesBalanceChange::added(amount)
        }

        // Exclude the reserved cycles.
        adjusted_balance_change =
            adjusted_balance_change + CyclesBalanceChange::added(self.reserved_cycles);

        // Apply the main cycles balance change without the consumed and reserved cycles.
        match adjusted_balance_change {
            CyclesBalanceChange::Added(added) => state.add_cycles(added),
            CyclesBalanceChange::Removed(removed) => state.remove_cycles(removed),
        }

        // Apply the consumed cycles with the use case metrics recording.
        let ConsumedCyclesDuringExecution {
            burned,
            instructions,
            request_and_response_transmission,
        } = self.consumed_cycles_by_use_case;
        if let Some(x) = burned {
            state.consume_cycles(x);
        }
        if let Some(x) = instructions {
            state.consume_cycles(x);
        }
        if let Some(x) = request_and_response_transmission {
            state.consume_cycles(x);
        }

        // Apply the reserved cycles. This must succeed because the cycle
        // changes were validated. If it doesn't succeed then, it is better to
        // crash here to avoid making the cycle balance incorrect.
        state.reserve_cycles(self.reserved_cycles).unwrap();

        // All changes applied above should be equivalent to simply applying
        // `self.cycles_balance_change` to the initial balance.
        let expected_balance = match self.cycles_balance_change {
            CyclesBalanceChange::Added(added) => initial_balance + added,
            CyclesBalanceChange::Removed(removed) => initial_balance - removed,
        };
        assert_eq!(state.balance(), expected_balance);
    }
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L2078-2082)
```rust
        if let Some(limit) = self.reserved_balance_limit {
            let requested = self.reserved_balance + amount;
            if requested > limit {
                return Err(ReservationError::ReservedLimitExceed { requested, limit });
            }
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L2097-2102)
```rust
    pub fn reserve_cycles(&mut self, amount: Cycles) -> Result<(), ReservationError> {
        self.can_reserve_cycles(amount, self.cycles_balance)?;
        self.cycles_balance -= amount;
        self.reserved_balance += amount;
        Ok(())
    }
```
