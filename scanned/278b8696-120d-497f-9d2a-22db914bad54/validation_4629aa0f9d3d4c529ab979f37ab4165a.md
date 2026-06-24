### Title
`ic0.cycles_burn128` During DTS Execution Can Cause Ingress Induction Fees to Be Silently Dropped - (`rs/replicated_state/src/canister_state/system_state.rs`)

### Summary
During Deterministic Time Slicing (DTS), when a canister has a paused execution, new ingress messages cannot immediately deduct their induction cost from the canister's balance. Instead, the cost is recorded as a pending `ingress_induction_cycles_debit`. However, the `ic0.cycles_burn128` system call — available during update execution — operates on the raw `cycles_balance` without accounting for this pending debit. A canister can burn cycles during its DTS execution to reduce its balance below the pending debit. When execution finishes and `apply_ingress_induction_cycles_debit` is called, the excess debit is silently dropped, causing ingress messages to be inducted for free.

### Finding Description

**Step 1 — Ingress induction cost is deferred as a postponed charge.**

When a canister has a paused DTS execution and a new ingress message arrives, `charge_ingress_induction_cost` detects the paused state and records the cost as a postponed charge: [1](#0-0) 

The guard at line 336 uses `debited_balance()` (i.e., `cycles_balance - ingress_induction_cycles_debit`) to ensure the canister can afford the charge at the time of deferral. The debit is accumulated in `ingress_induction_cycles_debit` without touching `cycles_balance`. [2](#0-1) 

**Step 2 — `ic0.cycles_burn128` ignores the pending debit.**

The `cycles_burn` function, called by the `ic0.cycles_burn128` system call during Wasm execution, only checks against the freeze threshold — it does not consider `ingress_induction_cycles_debit`: [3](#0-2) 

This means a canister can burn cycles down to `freeze_threshold`, reducing `cycles_balance` below the outstanding `ingress_induction_cycles_debit`.

**Step 3 — The excess debit is silently dropped at execution completion.**

When the DTS execution finishes, `apply_ingress_induction_cycles_debit` is called. If `ingress_induction_cycles_debit > cycles_balance`, the saturating subtraction produces a nonzero `remaining_debit`. The code explicitly acknowledges this as a bug path and **drops the remaining debit**: [4](#0-3) 

The `consume_cycles` call at line 1034 uses the full `ingress_induction_cycles_debit` value, but because `Cycles` arithmetic is saturating, the balance clamps to zero and the excess debit is never collected. The ingress messages that generated the debit were inducted without their fees being paid.

This is called at the end of both update and response callback execution: [5](#0-4) [6](#0-5) 

### Impact Explanation

A canister developer can deploy a canister whose update method intentionally runs long enough to trigger DTS (multi-round execution) and calls `ic0.cycles_burn128` to drain its balance to the freeze threshold. While the canister is paused, ingress messages sent to it accumulate as `ingress_induction_cycles_debit`. When execution completes, the debit exceeds the remaining balance and is silently dropped. The ingress induction fees — which are supposed to be burned — are never collected. This is a **cycles conservation violation**: cycles that should have been burned are not, inflating the effective supply. Repeated exploitation across many canisters could meaningfully distort the cycles economy.

### Likelihood Explanation

The attack requires: (1) deploying a canister with a long-running update method that calls `ic0.cycles_burn128`, and (2) having ingress messages arrive while the canister is paused. Both conditions are fully attacker-controlled. The canister developer controls the Wasm code and can time the burn. The ingress messages can be sent by the attacker themselves (or any user). No privileged access, governance majority, or threshold corruption is required. The `ic0.cycles_burn128` system call is available in `Update`, `ReplyCallback`, `RejectCallback`, `Cleanup`, `Init`, `PreUpgrade`, `ReplicatedQuery`, and `SystemTask` contexts. [7](#0-6) 

### Recommendation

`cycles_burn` should account for the pending `ingress_induction_cycles_debit` when computing the maximum burnable amount. The effective available balance for burning should be `debited_balance() - freeze_threshold` rather than `cycles_balance - freeze_threshold`. Alternatively, `cycles_burn128` should be disallowed or capped when `ingress_induction_cycles_debit > 0`, similar to how `debited_balance()` is used in `charge_ingress_induction_cost`. [8](#0-7) 

### Proof of Concept

```
1. Deploy canister C with an update method that:
   a. Loops for > 1 DTS slice (e.g., executes > slice_instruction_limit instructions)
   b. Calls ic0.cycles_burn128(balance - freeze_threshold) to drain to freeze threshold

2. Submit the update call to C → DTS begins, C is paused after first slice.

3. While C is paused, submit N ingress messages to C.
   → charge_ingress_induction_cost detects paused execution
   → each message's cost is added to ingress_induction_cycles_debit
   → debited_balance check passes (balance still appears sufficient)

4. C's execution resumes and burns cycles to freeze_threshold.
   → cycles_balance is now << ingress_induction_cycles_debit

5. Execution finishes → apply_ingress_induction_cycles_debit is called:
   → remaining_debit = ingress_induction_cycles_debit - cycles_balance > 0
   → error counter incremented, remaining debit DROPPED
   → N ingress messages were inducted without paying their fees
```

### Citations

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

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1045-1072)
```rust
    pub fn cycles_burn(
        &self,
        cycles_balance: &mut Cycles,
        amount_to_burn: Cycles,
        freeze_threshold: NumSeconds,
        memory_allocation: MemoryAllocation,
        memory_usage: NumBytes,
        message_memory_usage: MessageMemoryUsage,
        compute_allocation: ComputeAllocation,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
        reserved_balance: Cycles,
    ) -> Cycles {
        let threshold = self.freeze_threshold_cycles(
            freeze_threshold,
            memory_allocation,
            memory_usage,
            message_memory_usage,
            compute_allocation,
            subnet_cycles_config,
            reserved_balance,
        );

        // The subtraction '*cycles_balance - threshold' is saturating
        // and hence returned value will never be negative.
        let burning = min(amount_to_burn, *cycles_balance - threshold);

        *cycles_balance -= burning;
        burning
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L947-950)
```rust
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

**File:** rs/execution_environment/src/execution/call_or_task.rs (L483-490)
```rust
        self.canister
            .system_state
            .apply_ingress_induction_cycles_debit(
                self.canister.canister_id(),
                round.cost_schedule,
                round.log,
                round.counters.charging_from_balance_error,
            );
```

**File:** rs/execution_environment/src/execution/response.rs (L388-395)
```rust
        self.canister
            .system_state
            .apply_ingress_induction_cycles_debit(
                self.canister.canister_id(),
                round.cost_schedule,
                round.log,
                round.counters.charging_from_balance_error,
            );
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L4271-4286)
```rust
            ApiType::Init { .. }
            | ApiType::ReplicatedQuery { .. }
            | ApiType::PreUpgrade { .. }
            | ApiType::Cleanup { .. }
            | ApiType::Update { .. }
            | ApiType::SystemTask { .. }
            | ApiType::ReplyCallback { .. }
            | ApiType::RejectCallback { .. } => {
                let cycles = self.sandbox_safe_system_state.cycles_burn128(
                    amount,
                    self.memory_usage.current_usage,
                    self.memory_usage.current_message_usage,
                );
                copy_cycles_to_heap(cycles, dst, heap, method_name)?;
                Ok(())
            }
```
