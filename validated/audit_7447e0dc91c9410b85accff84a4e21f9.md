### Title
Ingress Cycle Check Ignores Pending `ingress_induction_cycles_debit`, Allowing Over-Admission of Messages During DTS Execution - (File: `rs/cycles_account_manager/src/cycles_account_manager.rs`)

---

### Summary

`can_withdraw_cycles_with_threshold` checks `system_state.balance()` (raw cycles balance) instead of `system_state.debited_balance()` (balance minus pending `ingress_induction_cycles_debit`). Both the boundary-node pre-filter (`should_accept_ingress_message`) and the consensus-level ingress selector (`validate_ingress`) call this function. As a result, when a canister is mid-DTS execution and has accumulated a large pending debit, ingress messages are admitted into blocks that will be rejected during actual induction — wasting block capacity and, in the edge case where the debit exceeds the remaining balance, silently forgiving some induction charges.

---

### Finding Description

The IC uses **Deterministic Time Slicing (DTS)** to spread long-running executions across multiple rounds. While a canister has a paused execution, new ingress induction costs cannot be immediately deducted from `cycles_balance` (because DTS requires the balance to remain stable during execution). Instead they are accumulated in `ingress_induction_cycles_debit` and applied when execution completes.

`SystemState` exposes two balance views:

- `balance()` — raw `cycles_balance`, does **not** subtract the pending debit
- `debited_balance()` — `cycles_balance - ingress_induction_cycles_debit`, the true available amount [1](#0-0) 

`charge_ingress_induction_cost` — the function that **actually charges** a canister during induction — correctly uses `debited_balance()` when the canister has a paused execution: [2](#0-1) 

However, `can_withdraw_cycles_with_threshold` — used for **pre-admission checks** — compares against `system_state.balance()` (the raw balance), ignoring the pending debit entirely: [3](#0-2) 

This function is called in two externally reachable paths:

**1. Boundary-node pre-filter** (`should_accept_ingress_message`): [4](#0-3) 

**2. Consensus ingress selector** (`validate_ingress`), which also accumulates a `cycles_needed` map across messages in the same block — but that map starts from zero and never accounts for the existing `ingress_induction_cycles_debit`: [5](#0-4) 

---

### Impact Explanation

**Scenario:** A canister has `cycles_balance = 1_000_000`, `ingress_induction_cycles_debit = 900_000` (accumulated during a long DTS execution), and `debited_balance = 100_000`. The freeze threshold is, say, `50_000`.

- The ingress selector sees `balance() = 1_000_000` and allows messages totalling up to `950_000` cycles to be included in a block.
- During actual induction (`charge_ingress_induction_cost`), the check uses `debited_balance() = 100_000`, so all but the first `~50_000`-cycle message fail with `CanisterOutOfCycles`.
- Those failed messages consumed block space (up to `max_ingress_messages_per_block`) without being inducted.

**Secondary impact:** `apply_ingress_induction_cycles_debit` contains an explicit fallback for the case where the debit exceeds the remaining balance: [6](#0-5) 

If cycles burned during execution push `cycles_balance` below `ingress_induction_cycles_debit`, the excess debit is silently dropped — meaning some ingress induction charges become free. This is a minor economic impact acknowledged as a known edge case by the comment `[EXC-BUG]`.

---

### Likelihood Explanation

Any canister running a long DTS execution (e.g., a large `install_code`, a compute-heavy update) will accumulate `ingress_induction_cycles_debit`. An unprivileged sender can observe that a canister is in DTS (e.g., by noticing delayed responses) and flood the ingress pool with messages targeting it. Those messages pass both the boundary-node check and the consensus ingress selector, consuming block capacity, but are rejected during induction. The attacker pays only the cost of submitting ingress messages; the victim canister's block quota is consumed.

---

### Recommendation

Replace `system_state.balance()` with `system_state.debited_balance()` in `can_withdraw_cycles_with_threshold`:

```rust
// Line 903 — change:
if threshold + requested > system_state.balance() {
// to:
if threshold + requested > system_state.debited_balance() {
```

Alternatively, add a separate `can_withdraw_cycles_with_threshold_debited` variant that accepts the debit and use it in the two pre-admission call sites. The `charge_ingress_induction_cost` path already handles this correctly and does not need to change.

---

### Proof of Concept

1. Deploy a canister `C` with `cycles_balance = 1_000_000` and a long-running update method (e.g., loops for many instructions to trigger DTS).
2. Call the long-running method; observe that `C` enters DTS (`has_paused_execution_or_install_code() == true`).
3. While `C` is paused, send many ingress messages to `C` from an unprivileged identity. Each message costs ~`10_000` cycles.
4. Observe that the boundary node accepts all messages (passes `can_withdraw_cycles_with_threshold` against raw `balance = 1_000_000`).
5. Observe that the ingress selector includes up to `max_ingress_messages_per_block` of them in a block.
6. Observe that during induction, `charge_ingress_induction_cost` checks `debited_balance()` and rejects all but the first few messages with `CanisterOutOfCycles`.
7. Block space is consumed by messages that were never inducted, confirming the over-admission.

The root cause is confirmed at:
- `rs/cycles_account_manager/src/cycles_account_manager.rs` line 903 (`balance()` instead of `debited_balance()`)
- `rs/execution_environment/src/execution_environment.rs` line 3358 (boundary-node call site)
- `rs/ingress_manager/src/ingress_selector.rs` line 570 (consensus call site)

### Citations

**File:** rs/replicated_state/src/canister_state/system_state.rs (L940-950)
```rust
    /// Returns the amount of cycles that the balance holds.
    pub fn balance(&self) -> Cycles {
        self.cycles_balance
    }

    /// Returns the balance after applying the pending 'ingress_induction_cycles_debit'.
    /// Returns 0 if the balance is smaller than the pending 'ingress_induction_cycles_debit'.
    pub fn debited_balance(&self) -> Cycles {
        // We rely on saturating operations of `Cycles` here.
        self.cycles_balance - self.ingress_induction_cycles_debit
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

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L883-914)
```rust
    pub fn can_withdraw_cycles_with_threshold(
        &self,
        system_state: &SystemState,
        requested: Cycles,
        canister_current_memory_usage: NumBytes,
        canister_current_message_memory_usage: MessageMemoryUsage,
        canister_reserved_balance: Cycles,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
        reveal_top_up: bool,
    ) -> Result<(), CanisterOutOfCyclesError> {
        let threshold = self.freeze_threshold_cycles(
            system_state.freeze_threshold,
            system_state.memory_allocation,
            canister_current_memory_usage,
            canister_current_message_memory_usage,
            system_state.compute_allocation,
            subnet_cycles_config,
            canister_reserved_balance,
        );

        if threshold + requested > system_state.balance() {
            Err(CanisterOutOfCyclesError {
                canister_id: system_state.canister_id(),
                available: system_state.balance(),
                requested,
                threshold,
                reveal_top_up,
            })
        } else {
            Ok(())
        }
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L3356-3373)
```rust
                if let Err(err) = self
                    .cycles_account_manager
                    .can_withdraw_cycles_with_threshold(
                        &paying_canister.system_state,
                        cost,
                        paying_canister.memory_usage(),
                        paying_canister.message_memory_usage(),
                        paying_canister.system_state.reserved_balance(),
                        subnet_cycles_config,
                        reveal_top_up,
                    )
                {
                    return Err(UserError::new(
                        ErrorCode::CanisterOutOfCycles,
                        err.to_string(),
                    ));
                }
            }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L565-584)
```rust
                Some(canister) => {
                    let cumulative_ingress_cost =
                        cycles_needed.entry(payer).or_insert_with(Cycles::zero);
                    if let Err(err) = self
                        .cycles_account_manager
                        .can_withdraw_cycles_with_threshold(
                            &canister.system_state,
                            *cumulative_ingress_cost + ingress_cost,
                            canister.memory_usage(),
                            canister.message_memory_usage(),
                            canister.system_state.reserved_balance(),
                            subnet_cycles_config,
                            false, // error here is not returned back to the user => no need to reveal top up balance
                        )
                    {
                        return Err(ValidationError::InvalidArtifact(
                            InvalidIngressPayloadReason::InsufficientCycles(err),
                        ));
                    }
                    *cumulative_ingress_cost += ingress_cost;
```
