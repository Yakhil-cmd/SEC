### Title
Ingress Cycle Balance Check Ignores Pending `ingress_induction_cycles_debit` During DTS Paused Execution - (`rs/cycles_account_manager/src/cycles_account_manager.rs`, `rs/ingress_manager/src/ingress_selector.rs`)

### Summary

The `can_withdraw_cycles_with_threshold` function checks `system_state.balance()` (the raw cycle balance) rather than `system_state.debited_balance()` (balance minus pending `ingress_induction_cycles_debit`). This function is used in both the ingress filter pre-check and the ingress selector's rigorous block-building check. The actual induction path in `valid_set_rule.rs` correctly uses `debited_balance()` when a canister has a paused execution. The discrepancy means the ingress selector can include messages in a block that will be rejected at induction time, because the selector does not account for the portion of the balance already committed to pending DTS charges.

### Finding Description

During Deterministic Time Slicing (DTS), when a canister has a paused execution, new ingress induction costs are not immediately deducted from `cycles_balance`. Instead they are accumulated in `ingress_induction_cycles_debit` and applied only when the paused execution completes. The "true available" balance is therefore `debited_balance() = cycles_balance - ingress_induction_cycles_debit`.

`can_withdraw_cycles_with_threshold` compares against the raw balance:

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs
if threshold + requested > system_state.balance() {   // ← uses balance(), not debited_balance()
```

This function is called in two places that gate ingress acceptance:

1. **First-pass filter** in `should_accept_ingress_message` (comment: "A more rigorous check happens later in the ingress selector"):

```rust
// rs/execution_environment/src/execution_environment.rs
self.cycles_account_manager
    .can_withdraw_cycles_with_threshold(
        &paying_canister.system_state,
        cost, ...
    )
```

2. **Block-building selector** in `ingress_selector.rs`, which also accumulates cumulative costs but still calls the same function:

```rust
// rs/ingress_manager/src/ingress_selector.rs
self.cycles_account_manager
    .can_withdraw_cycles_with_threshold(
        &canister.system_state,
        *cumulative_ingress_cost + ingress_cost, ...
    )
```

By contrast, the actual induction in `valid_set_rule.rs` calls `charge_ingress_induction_cost`, which correctly branches on `debited_balance()` when a paused execution is present:

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs
if canister.has_paused_execution_or_install_code() {
    if canister.system_state.debited_balance() < cycles + threshold {
        return Err(...);
    }
    canister.system_state
        .add_postponed_charge_to_ingress_induction_cycles_debit(cycles);
```

The result is a systematic mismatch: the selector admits messages using the inflated `balance()`, but induction rejects them using the correct `debited_balance()`.

### Impact Explanation

Messages selected for a block but rejected at induction waste block payload capacity. Because the ingress selector tracks cumulative costs per payer using `balance()`, it may admit N messages for a canister whose `debited_balance()` can only afford M < N of them. All N − M excess messages consume block space and are then silently dropped at induction. Under sustained DTS activity (long-running Wasm, large stable-memory operations), the gap between `balance()` and `debited_balance()` can be substantial, amplifying the waste. Additionally, `apply_ingress_induction_cycles_debit` contains an explicit "unreachable" error path that silently forgives any debit that exceeds the balance at settlement time, meaning edge-case over-induction can result in some ingress fees being collected for free.

### Likelihood Explanation

The condition requires a canister to have an active paused execution (DTS), which occurs naturally for any canister executing a message that exceeds the per-round instruction slice limit. Any unprivileged ingress sender can observe that a canister is in a long-running execution (e.g., via `canister_status` or by timing responses) and then flood it with ingress messages. The ingress selector will accept more messages than the canister can afford, and induction will reject the excess. No privileged access is required; the attacker only needs to send standard ingress messages.

### Recommendation

`can_withdraw_cycles_with_threshold` should use `system_state.debited_balance()` instead of `system_state.balance()` when the canister has a paused execution, mirroring the logic already present in `charge_ingress_induction_cost`. Alternatively, the ingress selector should call `charge_ingress_induction_cost` (or a read-only equivalent) to obtain a consistent affordability estimate.

### Proof of Concept

1. Deploy a canister with a Wasm that performs a large stable-memory operation (e.g., `stable64_grow(10_000)`) so that it reliably triggers DTS and accumulates a non-zero `ingress_induction_cycles_debit`.
2. While the canister is paused, send a burst of ingress messages whose total induction cost exceeds `debited_balance()` but is below `balance()`.
3. Observe that the ingress selector includes all messages in the block (checked via `can_withdraw_cycles_with_threshold` against `balance()`), but `valid_set_rule::enqueue` rejects the excess messages at induction time (checked via `charge_ingress_induction_cost` against `debited_balance()`).
4. Confirm that block payload is consumed by the rejected messages, reducing effective subnet throughput. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

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

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L883-913)
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
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L940-955)
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

    /// Returns the pending 'ingress_induction_cycles_debit'.
    pub fn ingress_induction_cycles_debit(&self) -> Cycles {
        self.ingress_induction_cycles_debit
    }
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

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1011-1038)
```rust
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
```

**File:** rs/execution_environment/src/execution_environment.rs (L3340-3373)
```rust
        // A first-pass check on the canister's balance to prevent needless gossiping
        // if the canister's balance is too low. A more rigorous check happens later
        // in the ingress selector.
        {
            let subnet_cycles_config = state.get_own_subnet_cycles_config();
            let induction_cost = self.cycles_account_manager.ingress_induction_cost(
                ingress,
                effective_canister_id,
                subnet_cycles_config,
            );

            if let IngressInductionCost::Fee { payer, cost } = induction_cost {
                let paying_canister = canister(payer)?;
                let reveal_top_up = paying_canister
                    .controllers()
                    .contains(&ingress.sender().get());
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

**File:** rs/ingress_manager/src/ingress_selector.rs (L551-595)
```rust
        // Skip the message if there aren't enough cycles to induct the message.
        let effective_canister_id = extract_effective_canister_id(msg).map_err(|_| {
            ValidationError::InvalidArtifact(InvalidIngressPayloadReason::InvalidManagementMessage)
        })?;
        let subnet_cycles_config = state.get_own_subnet_cycles_config();
        match self.cycles_account_manager.ingress_induction_cost(
            signed_ingress,
            effective_canister_id,
            subnet_cycles_config,
        ) {
            IngressInductionCost::Fee {
                payer,
                cost: ingress_cost,
            } => match state.canister_state(&payer) {
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
                }
                None => {
                    return Err(ValidationError::InvalidArtifact(
                        InvalidIngressPayloadReason::CanisterNotFound(payer),
                    ));
                }
            },
            IngressInductionCost::Free => {
                // Do nothing.
            }
        };
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L293-336)
```rust
            IngressInductionCost::Fee { payer, cost } => {
                // Get the paying canister from the state.
                let canister = match state.canister_state_make_mut(&payer) {
                    Some(canister) => canister,
                    None => return Err(IngressInductionError::CanisterNotFound(payer)),
                };

                // Withdraw cost of inducting the message.
                let memory_usage = canister.memory_usage();
                let message_memory_usage = canister.message_memory_usage();
                let compute_allocation = canister.compute_allocation();
                let reveal_top_up = canister.controllers().contains(&ingress.source.get());
                if let Err(err) = self.cycles_account_manager.charge_ingress_induction_cost(
                    canister,
                    memory_usage,
                    message_memory_usage,
                    compute_allocation,
                    cost,
                    subnet_cycles_config,
                    reveal_top_up,
                ) {
                    return Err(IngressInductionError::CanisterOutOfCycles(err));
                }

                // Ensure the canister is running if the message isn't to a subnet.
                if !ingress.is_addressed_to_subnet() {
                    match canister.status() {
                        CanisterStatusType::Running => {}
                        CanisterStatusType::Stopping => {
                            return Err(IngressInductionError::CanisterStopping(
                                canister.canister_id(),
                            ));
                        }
                        CanisterStatusType::Stopped => {
                            return Err(IngressInductionError::CanisterStopped(
                                canister.canister_id(),
                            ));
                        }
                    }
                }

                state.push_ingress(ingress)
            }
        }
```
