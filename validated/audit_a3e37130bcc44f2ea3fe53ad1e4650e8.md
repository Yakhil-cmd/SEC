Audit Report

## Title
Ingress Pre-Check Uses Stale `balance()` Instead of `debited_balance()` During DTS, Allowing Zero-Cost Block Space Waste - (File: `rs/cycles_account_manager/src/cycles_account_manager.rs`)

## Summary
`can_withdraw_cycles_with_threshold` compares the requested ingress cost against `system_state.balance()` (the raw cycle balance), which does not subtract the pending `ingress_induction_cycles_debit` accumulated during a Deterministic Time Slicing (DTS) paused execution. The downstream induction function `charge_ingress_induction_cost` correctly uses `debited_balance()` for the same check. This mismatch allows ingress messages to pass the block-proposal pre-check and be included in a block, only to be rejected during actual induction, wasting block space at zero cost to the attacker.

## Finding Description
`SystemState` exposes two balance views: `balance()` returns the raw `cycles_balance` field, which is frozen during a multi-round DTS execution; `debited_balance()` returns `cycles_balance − ingress_induction_cycles_debit`, the true spendable amount.

During a paused DTS execution, each ingress message inducted against the canister does not immediately deduct from `cycles_balance`; instead the cost is accumulated in `ingress_induction_cycles_debit` via `add_postponed_charge_to_ingress_induction_cycles_debit`.

`charge_ingress_induction_cost` is aware of this and gates on `debited_balance()` when `has_paused_execution_or_install_code()` is true: [1](#0-0) 

However, `can_withdraw_cycles_with_threshold` — called by the ingress selector for every candidate message — always compares against `system_state.balance()`: [2](#0-1) 

The ingress selector accumulates per-payer costs and calls this function before including each message: [3](#0-2) 

Because `balance()` ignores the accumulated `ingress_induction_cycles_debit`, the pre-check sees a higher available balance than actually exists. Any message whose cost falls in the range `(debited_balance() − freeze_threshold, balance() − freeze_threshold]` passes the pre-check, is included in the block, and is then rejected by `charge_ingress_induction_cost` during induction. The `cumulative_ingress_cost` tracker in the selector is updated on pre-check success, so multiple such messages can be stacked in a single block payload.

## Impact Explanation
An attacker who controls (or observes) a canister in a paused DTS state can submit ingress messages that consume block space at zero cycles cost. The messages are rejected during induction so the canister is not overcharged, but the block space is permanently wasted. Because the ingress selector's `cumulative_ingress_cost` accumulates against the stale `balance()`, the attacker can stack multiple such messages per block, amplifying throughput degradation. This constitutes a **subnet availability impact not based on raw volumetric DDoS** — the attacker exploits a logic flaw in the block-proposal pipeline to degrade effective subnet throughput without paying for it. This matches the **Medium** bounty impact tier: a meaningful security impact requiring specific but achievable conditions (canister in paused DTS state).

## Likelihood Explanation
DTS is active on all application subnets for any message exceeding a single-round instruction slice. Any canister owner can deliberately trigger a paused DTS state by submitting a long-running update, heartbeat, or timer. Once paused, the attacker submits ingress messages to accumulate `ingress_induction_cycles_debit`, then submits additional messages sized to fall in the balance gap. No privileged access, key material, or subnet-majority corruption is required — only the ability to send ingress messages and own or observe a canister in a paused state. The attack is repeatable across rounds as long as the canister remains paused.

## Recommendation
Replace `system_state.balance()` with `system_state.debited_balance()` in `can_withdraw_cycles_with_threshold` at line 903, and update the `available` field in the returned `CanisterOutOfCyclesError` accordingly. Alternatively, the ingress selector call site should subtract `system_state.ingress_induction_cycles_debit()` from the balance before passing it to the threshold check, mirroring the logic already present in `charge_ingress_induction_cost`. The fix should also be validated against the `cumulative_ingress_cost` accumulation path to ensure the stacked-message scenario is also closed. [4](#0-3) 

## Proof of Concept
**Setup:**
- Canister C: `cycles_balance = 1_000`, `freeze_threshold_cycles = 500`.
- Attacker sends a long-running update to C, triggering DTS (C is now paused, `has_paused_execution_or_install_code() == true`).
- While C is paused, two ingress messages are inducted with postponed charges: `ingress_induction_cycles_debit = 400`.
- State: `balance() = 1_000`, `debited_balance() = 600`.

**Exploit step:**
- Attacker submits a third ingress message with `ingress_cost = 200`.
- Ingress selector calls `can_withdraw_cycles_with_threshold(requested = 200)`:
  - Uses `balance() = 1_000`; check: `500 + 200 = 700 ≤ 1_000` → **PASSES** (stale balance).
- Message is included in the block; `cumulative_ingress_cost += 200`.

**Induction step:**
- `charge_ingress_induction_cost` is called; canister has paused execution.
- Uses `debited_balance() = 600`; check: `600 < 200 + 500 = 700` → **FAILS** (correct).
- Message is rejected; canister is not charged.
- Block space is wasted; attacker's message was included at zero cost.

**Reproducible test plan:** Write a unit test in `rs/cycles_account_manager/src/cycles_account_manager.rs` or `rs/ingress_manager/src/ingress_selector.rs` that constructs a `SystemState` with `cycles_balance = 1_000`, `ingress_induction_cycles_debit = 400`, `freeze_threshold_cycles = 500`, marks the canister as having a paused execution, and asserts that `can_withdraw_cycles_with_threshold(requested = 200)` returns `Err` (it currently returns `Ok`, demonstrating the bug). [5](#0-4) [6](#0-5)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L335-344)
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

**File:** rs/replicated_state/src/canister_state/system_state.rs (L542-549)
```rust
    /// Pending charges to `cycles_balance` that are not applied yet.
    ///
    /// Deterministic time slicing requires that `cycles_balance` remains the
    /// same throughout a multi-round execution. During that time all charges
    /// performed in ingress induction are recorded in
    /// `ingress_induction_cycles_debit`. When the multi-round execution
    /// completes, it will apply `ingress_induction_cycles_debit` to `cycles_balance`.
    ingress_induction_cycles_debit: Cycles,
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L947-950)
```rust
    pub fn debited_balance(&self) -> Cycles {
        // We rely on saturating operations of `Cycles` here.
        self.cycles_balance - self.ingress_induction_cycles_debit
    }
```
