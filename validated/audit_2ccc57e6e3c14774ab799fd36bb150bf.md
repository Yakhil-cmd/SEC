### Title
Cycles Charged Before Canister Status Check in `enqueue()` Allows Unrefunded Cycles Drain on Stopping/Stopped Canisters - (File: rs/messaging/src/scheduling/valid_set_rule.rs)

---

### Summary

In `ValidSetRuleImpl::enqueue()`, the ingress induction cost is charged from the canister's cycles balance **before** checking whether the canister is `Stopping` or `Stopped`. When the subsequent status check fails, the already-charged cycles are **not refunded**, permanently draining cycles from the canister even though the message is rejected. This is the direct IC analog of the "Accruing Debt in Deprecated Bucket" pattern: debt (cycles) is accrued against a deprecated/inactive resource before its status is validated, and the implicit protection elsewhere in the pipeline does not cover all reachable paths.

---

### Finding Description

In `rs/messaging/src/scheduling/valid_set_rule.rs`, the `enqueue()` function handles the `IngressInductionCost::Fee` branch as follows:

1. **Charge first** (line 305–315): `charge_ingress_induction_cost()` is called, which either immediately consumes cycles from `cycles_balance` (if no paused execution) or adds to `ingress_induction_cycles_debit` (if paused execution exists).
2. **Status check after** (lines 317–332): Only after the charge succeeds does the code check whether the canister is `Stopping` or `Stopped`. If so, it returns `Err(IngressInductionError::CanisterStopping/Stopped)`.
3. **No refund**: The error path does not refund the cycles already charged in step 1. [1](#0-0) 

The analogous implicit protection exists in two places:
- `should_accept_ingress_message()` in `rs/execution_environment/src/execution_environment.rs` (lines 3387–3401) checks status before accepting the message at the boundary node filter stage.
- `validate_ingress()` in `rs/ingress_manager/src/ingress_selector.rs` (lines 536–548) checks status before including messages in blocks. [2](#0-1) [3](#0-2) 

However, these checks operate on a **snapshot of state at proposal/filter time**. A canister can transition from `Running` to `Stopping` between that snapshot and the actual execution of `enqueue()` — specifically, when a `stop_canister` management call and an ingress message to the same canister appear in the same block. Management/subnet messages are processed before ingress messages in a round, so the ordering is deterministic: `stop_canister` runs first, transitions the canister to `Stopping`, and then `enqueue()` is called for the ingress message against a now-`Stopping` canister.

The `charge_ingress_induction_cost()` function, when the canister has no paused execution, immediately consumes cycles via `consume_with_threshold`. When the canister has a paused execution, it adds to `ingress_induction_cycles_debit`. In neither case is the charge reversed when `enqueue()` returns an error. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

Cycles are permanently drained from a `Stopping` or `Stopped` canister for each ingress message that passes the pre-induction filters but reaches `enqueue()` after the canister has transitioned state. The canister pays the ingress induction fee for a message it never processes. This violates cycles conservation: the IC charges for a service not rendered. In the `has_paused_execution_or_install_code()` branch, the `ingress_induction_cycles_debit` is incremented and never decremented on the error path, meaning the debit will be applied to the balance when the paused execution completes — further compounding the loss.

**Vulnerability class**: Cycles/resource accounting bug.

---

### Likelihood Explanation

The race condition is realistic and deterministic. Any block containing both a `stop_canister` call and an ingress message to the same canister will trigger this path, since subnet messages are always processed before ingress messages within a round. A canister controller (or any principal who can call `stop_canister` on a canister they control) can deliberately construct such a block. This is an unprivileged ingress sender scenario: the attacker only needs to be a controller of the target canister, which is a standard user role.

---

### Recommendation

Move the canister status check **before** the `charge_ingress_induction_cost()` call in `enqueue()`. The check at lines 317–332 should precede line 305. This mirrors the fix described in the external report: validate the resource's active status before allowing any debt/charge to be accrued against it. Alternatively, if the status check fails after charging, explicitly refund the charged amount by calling `remove_charge_from_ingress_induction_cycles_debit()` or the equivalent balance restoration. [6](#0-5) 

---

### Proof of Concept

1. Attacker controls canister `C` with a non-zero cycles balance.
2. Attacker constructs a block containing:
   - A `stop_canister` management call targeting `C` (processed first as a subnet message in the round).
   - An ingress message to `C` (processed after subnet messages).
3. During block execution, `stop_canister` transitions `C` to `CanisterStatus::Stopping`.
4. `enqueue()` is called for the ingress message to `C`.
5. `charge_ingress_induction_cost()` succeeds — cycles are deducted from `C`'s balance (or added to `ingress_induction_cycles_debit`).
6. The status check at line 319 detects `CanisterStatusType::Stopping`, returns `Err(IngressInductionError::CanisterStopping(C))`.
7. No refund occurs. `C` has permanently lost cycles proportional to the ingress message size for a message it never executed.

Repeating this across multiple blocks (while `C` remains in `Stopping` state waiting for open call contexts to close) amplifies the cycles drain. [7](#0-6)

### Citations

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L293-335)
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
```

**File:** rs/execution_environment/src/execution_environment.rs (L3387-3401)
```rust
        match canister_state.status() {
            CanisterStatusType::Running => {}
            CanisterStatusType::Stopping => {
                return Err(UserError::new(
                    ErrorCode::CanisterStopping,
                    format!("Canister {} is stopping", ingress.canister_id()),
                ));
            }
            CanisterStatusType::Stopped => {
                return Err(UserError::new(
                    ErrorCode::CanisterStopped,
                    format!("Canister {} is stopped", ingress.canister_id()),
                ));
            }
        }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L527-549)
```rust
        // Do not include the message if the recipient is Stopping or Stopped.
        let msg = signed_ingress.content();
        if !msg.is_addressed_to_subnet() {
            let canister_id = msg.canister_id();
            let canister_state = state.canister_state(&canister_id).ok_or({
                ValidationError::InvalidArtifact(InvalidIngressPayloadReason::CanisterNotFound(
                    canister_id,
                ))
            })?;
            match canister_state.status() {
                CanisterStatusType::Running => {}
                CanisterStatusType::Stopping => {
                    return Err(ValidationError::InvalidArtifact(
                        InvalidIngressPayloadReason::CanisterStopping(canister_id),
                    ));
                }
                CanisterStatusType::Stopped => {
                    return Err(ValidationError::InvalidArtifact(
                        InvalidIngressPayloadReason::CanisterStopped(canister_id),
                    ));
                }
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

**File:** rs/replicated_state/src/canister_state/system_state.rs (L998-1005)
```rust
    /// Removes a previously postponed charge for ingress messages from the balance
    /// of the canister.
    ///
    /// Note that this will saturate the balance to zero if the charge to remove is
    /// larger than the current debit.
    pub fn remove_charge_from_ingress_induction_cycles_debit(&mut self, charge: Cycles) {
        self.ingress_induction_cycles_debit -= charge;
    }
```
