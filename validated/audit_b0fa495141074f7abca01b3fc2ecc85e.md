### Title
Ingress Induction Cycles Check Uses Raw Balance Instead of Debited Balance, Allowing Under-Funded Messages Into Blocks - (File: rs/cycles_account_manager/src/cycles_account_manager.rs)

### Summary

The `can_withdraw_cycles_with_threshold` function, used by both the ingress filter (`should_accept_ingress_message`) and the ingress selector (`validate_ingress`), checks a canister's raw `balance()` rather than its `debited_balance()` (i.e., `balance - ingress_induction_cycles_debit`). When a canister has a paused Deterministic Time Slicing (DTS) execution, pending ingress induction charges accumulate in `ingress_induction_cycles_debit` without being deducted from the raw balance. The pre-admission check therefore passes for messages the canister cannot actually afford, allowing them to be included in consensus blocks, only to be rejected at actual induction time.

### Finding Description

The IC uses DTS to spread long-running canister executions across multiple rounds. During a paused execution, ingress induction fees cannot be immediately deducted from `cycles_balance`; instead they are recorded in `ingress_induction_cycles_debit` and applied when execution resumes. The `debited_balance()` accessor correctly reflects this:

```rust
pub fn debited_balance(&self) -> Cycles {
    self.cycles_balance - self.ingress_induction_cycles_debit
}
``` [1](#0-0) 

However, `can_withdraw_cycles_with_threshold` — the function used for all pre-admission cycle checks — compares against the raw `system_state.balance()`:

```rust
if threshold + requested > system_state.balance() {
    Err(...)
} else {
    Ok(())
}
``` [2](#0-1) 

This function is called in two places that gate ingress message admission:

**1. Ingress filter** (`should_accept_ingress_message`), which runs on the latest certified state and decides whether to gossip/accept a message: [3](#0-2) 

**2. Ingress selector** (`validate_ingress`), which is the "more rigorous" check used during block building and validation: [4](#0-3) 

By contrast, the actual charging function `charge_ingress_induction_cost` correctly uses `debited_balance()` when the canister has a paused execution:

```rust
if canister.has_paused_execution_or_install_code() {
    if canister.system_state.debited_balance() < cycles + threshold {
        return Err(...);
    }
    canister.system_state.add_postponed_charge_to_ingress_induction_cycles_debit(cycles);
``` [5](#0-4) 

The pre-admission check is therefore strictly weaker than the actual charge check. A canister with:
- `balance = B`, `ingress_induction_cycles_debit = D`, `freeze_threshold_cycles = T`, ingress cost `C`

passes the pre-check when `T + C ≤ B`, but the actual charge requires `T + C ≤ B − D`. When `D > 0`, the pre-check passes but the actual charge fails.

### Impact Explanation

An unprivileged ingress sender can craft messages targeting a canister that is mid-DTS-execution (i.e., has a non-zero `ingress_induction_cycles_debit`). These messages pass both the ingress filter and the ingress selector's cycle check, are included in consensus blocks, and are then rejected at induction time because `charge_ingress_induction_cost` uses the correct `debited_balance()`. The result is:

- **Block space exhaustion**: Rejected-at-induction messages still consume block message slots, reducing effective subnet throughput.
- **No cycles cost to attacker**: The receiving canister pays nothing for rejected messages; the attacker pays only the (zero or negligible) cost of submitting ingress messages.
- **Repeated exploitation**: As long as a target canister remains in a paused DTS state, the attacker can continuously fill blocks with messages that will be rejected.

### Likelihood Explanation

DTS paused executions are a normal operational state for any canister running a long update call or install_code. The `ingress_induction_cycles_debit` field is part of the replicated state and is observable indirectly (e.g., by monitoring `canister_status` cycle balance vs. expected burn). Any unprivileged user can submit ingress messages to any running canister. No privileged access, key material, or majority corruption is required.

### Recommendation

Replace `system_state.balance()` with `system_state.debited_balance()` inside `can_withdraw_cycles_with_threshold`, or add an overload that accepts the debited balance. Alternatively, pass `debited_balance()` as the effective balance at the call sites in `should_accept_ingress_message` and `validate_ingress`:

```rust
// In can_withdraw_cycles_with_threshold or at call sites:
let effective_balance = system_state.debited_balance(); // was: system_state.balance()
if threshold + requested > effective_balance {
    Err(...)
} else {
    Ok(())
}
```

This aligns the pre-admission check with the invariant enforced by `charge_ingress_induction_cost`, ensuring that messages admitted into blocks can always be successfully inducted.

### Proof of Concept

1. Deploy canister C with balance `B = freeze_threshold + ingress_cost + 1` cycles.
2. Trigger a long DTS update call on C so that `ingress_induction_cycles_debit = D` accumulates (e.g., `D = 2`), making `debited_balance = B − D < freeze_threshold + ingress_cost`.
3. While C is paused, submit an ingress message to C with cost `ingress_cost`.
4. Observe: `should_accept_ingress_message` returns `Ok(())` (check uses `balance() = B`).
5. Observe: `validate_ingress` in the ingress selector also passes (same check).
6. The message is included in the next block.
7. At induction, `charge_ingress_induction_cost` calls `debited_balance()`, finds it insufficient, and rejects the message.
8. Repeat step 3–7 to fill blocks with rejected messages, exhausting block message capacity.

### Citations

**File:** rs/replicated_state/src/canister_state/system_state.rs (L945-950)
```rust
    /// Returns the balance after applying the pending 'ingress_induction_cycles_debit'.
    /// Returns 0 if the balance is smaller than the pending 'ingress_induction_cycles_debit'.
    pub fn debited_balance(&self) -> Cycles {
        // We rely on saturating operations of `Cycles` here.
        self.cycles_balance - self.ingress_induction_cycles_debit
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

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L903-913)
```rust
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

**File:** rs/ingress_manager/src/ingress_selector.rs (L568-584)
```rust
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
