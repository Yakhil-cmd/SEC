### Title
Ingress Payload Batch Validation Ignores Pending DTS Cycles Debit, Allowing Overspending - (File: `rs/ingress_manager/src/ingress_selector.rs`)

---

### Summary

The ingress payload validator (`validate_ingress_payload`) and payload builder (`get_ingress_payload`) check canister cycle affordability using `system_state.balance()` (raw `cycles_balance`), while the actual ingress induction path uses `system_state.debited_balance()` (`cycles_balance - ingress_induction_cycles_debit`) when the canister has a paused Deterministic Time Slicing (DTS) execution. This is a direct analog to the reported batch-ordering bug: intermediate state (the pending debit) is not reflected during batch validation, so subsequent messages in the same block are validated against a stale, inflated balance. Multiple ingress messages that the canister cannot actually afford are included in a block and all fail during induction.

---

### Finding Description

**Root cause — validation path:**

In `validate_ingress` the cycles check is:

```rust
// rs/ingress_manager/src/ingress_selector.rs  lines 566-584
let cumulative_ingress_cost =
    cycles_needed.entry(payer).or_insert_with(Cycles::zero);
if let Err(err) = self
    .cycles_account_manager
    .can_withdraw_cycles_with_threshold(
        &canister.system_state,          // ← reads certified-state snapshot
        *cumulative_ingress_cost + ingress_cost,
        ...
    )
{ ... }
*cumulative_ingress_cost += ingress_cost;
```

`can_withdraw_cycles_with_threshold` evaluates:

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs  lines 903-913
if threshold + requested > system_state.balance() {   // ← raw cycles_balance
    Err(...)
} else { Ok(()) }
``` [1](#0-0) [2](#0-1) 

**Root cause — induction path:**

The actual induction in `charge_ingress_induction_cost` takes a different branch when the canister has a paused execution:

```rust
// rs/cycles_account_manager/src/cycles_account_manager.rs  lines 335-348
if canister.has_paused_execution_or_install_code() {
    if canister.system_state.debited_balance() < cycles + threshold {
        return Err(CanisterOutOfCyclesError { ... });
    }
    canister.system_state
        .add_postponed_charge_to_ingress_induction_cycles_debit(cycles);
    Ok(())
}
``` [3](#0-2) 

**The debit field:**

```rust
// rs/replicated_state/src/canister_state/system_state.rs  lines 542-549
/// Pending charges to `cycles_balance` that are not applied yet.
/// Deterministic time slicing requires that `cycles_balance` remains the
/// same throughout a multi-round execution. During that time all charges
/// performed in ingress induction are recorded in
/// `ingress_induction_cycles_debit`.
ingress_induction_cycles_debit: Cycles,
```

`debited_balance()` = `cycles_balance − ingress_induction_cycles_debit`. [4](#0-3) [5](#0-4) 

**The batch-ordering gap (analog to the report):**

The `cycles_needed` accumulator correctly sums costs across messages in the same block, but it is checked against `balance()` — the raw balance that does not subtract the already-committed `ingress_induction_cycles_debit`. The debit is only applied when the DTS execution finishes (`apply_ingress_induction_cycles_debit`), not during block building. So every message in the batch is validated against a balance that is inflated by the full pending debit, exactly mirroring the Solidity bug where redistribution is deferred to the end of the batch. [6](#0-5) [7](#0-6) 

The same inconsistency exists in both `get_ingress_payload` (payload building) and `validate_ingress_payload` (payload validation): [8](#0-7) [9](#0-8) 

---

### Impact Explanation

**Cycles accounting divergence:** A canister with `cycles_balance = B` and `ingress_induction_cycles_debit = D` (where `D` is large, e.g. from a long `install_code` or update) has an effective spendable balance of `B − D`. The validator sees `B`; the inductor sees `B − D`. Every ingress message in the block that passes validation against `B` but exceeds `B − D` is inducted and immediately rejected with `CanisterOutOfCycles`, consuming block space without delivering any work.

**Block-space exhaustion (DoS):** An unprivileged sender can trigger this by:
1. Deploying a canister and initiating a long DTS execution (large `install_code` or a compute-heavy update), causing `ingress_induction_cycles_debit` to grow.
2. Submitting many ingress messages to that canister while the execution is paused.
3. The ingress selector includes them (validation passes against `balance()`); all fail during induction (inductor uses `debited_balance()`).

Because the ingress induction cost is charged to the *canister*, not the submitter, the attacker bears no per-message cost for the failing messages. The per-canister quota in the round-robin selector limits the fraction of a single block that can be wasted, but the attack can be sustained across many blocks for the duration of the DTS execution.

---

### Likelihood Explanation

- DTS executions are a normal, production feature triggered by any sufficiently large update or `install_code` call.
- Any unprivileged ingress sender can submit messages to any canister.
- No privileged role, key material, or subnet-majority corruption is required.
- The certified state (used by the ingress selector) always contains the current `ingress_induction_cycles_debit`, so the correct value is available but unused.
- Likelihood: **Medium** — requires deliberate setup (triggering a long DTS execution) but is fully reachable by an unprivileged actor.

---

### Recommendation

In `validate_ingress`, replace the call to `can_withdraw_cycles_with_threshold` (which uses `balance()`) with a check that mirrors `charge_ingress_induction_cost`: when `canister.has_paused_execution_or_install_code()` is true, check against `debited_balance()` instead of `balance()`. Alternatively, expose a dedicated helper in `CyclesAccountManager` that replicates the exact branch logic of `charge_ingress_induction_cost` so that validation and induction are guaranteed to stay in sync. [10](#0-9) 

---

### Proof of Concept

```
1. Deploy canister C on an application subnet.
   Give C exactly enough cycles to cover the freeze threshold plus
   one ingress induction cost (call it `cost`).

2. Send C a large update message that will run for many DTS rounds.
   After the first slice executes, C's `ingress_induction_cycles_debit`
   equals `cost` (charged by `charge_ingress_induction_cost` via the
   DTS path), so `debited_balance() == 0`.

3. While C's execution is paused, submit two ingress messages M1, M2
   each with induction cost `cost/2` from any user principal.

4. Observe that both M1 and M2 appear in the next finalized block
   (validation passes: `balance() = cost`, cumulative check
   `cost/2 <= cost` and `cost <= cost` both succeed).

5. Observe that both M1 and M2 are rejected during induction with
   `CanisterOutOfCycles` (inductor checks `debited_balance() = 0 < cost/2`).

6. Block space is consumed; neither message is executed; the attacker
   paid no induction fee.
```

### Citations

**File:** rs/ingress_manager/src/ingress_selector.rs (L89-116)
```rust
        let state = match self.state_reader.get_state_at(certified_height) {
            Ok(state) => state,
            Err(err) => {
                warn!(
                    every_n_seconds => 5,
                    self.log,
                    "StateManager doesn't have state for height {}: {:?}", certified_height, err
                );

                return PayloadWithSizeEstimate::default();
            }
        }
        .take();

        let min_expiry = context.time;
        let max_expiry = context.time + MAX_INGRESS_TTL;
        let expiry_range = min_expiry..=max_expiry;

        let settings = self
            .get_ingress_message_settings(context.registry_version)
            .expect("Couldn't fetch ingress message parameters from the registry.");

        // Select valid ingress messages and stop once the total size
        // becomes greater than byte_limit.
        let mut accumulated_wire_size = NumBytes::new(0);
        let mut accumulated_memory_size = NumBytes::new(0);
        let mut cycles_needed: BTreeMap<CanisterId, Cycles> = BTreeMap::new();

```

**File:** rs/ingress_manager/src/ingress_selector.rs (L358-418)
```rust
        let state = match self.state_reader.get_state_at(certified_height) {
            Ok(state) => state.take(),
            Err(err) => {
                warn!(
                    every_n_seconds => 30,
                    self.log,
                    "StateManager doesn't have state for height {}: {:?}", certified_height, err
                );

                return Err(ValidationError::ValidationFailed(
                    IngressPayloadValidationFailure::StateManagerError(certified_height, err),
                ));
            }
        };

        if payload.message_count() > settings.max_ingress_messages_per_block {
            return Err(ValidationError::InvalidArtifact(
                InvalidIngressPayloadReason::IngressPayloadTooManyMessages(
                    payload.message_count(),
                    settings.max_ingress_messages_per_block,
                ),
            ));
        }

        // Tracks the sum of cycles needed per canister.
        let mut cycles_needed: BTreeMap<CanisterId, Cycles> = BTreeMap::new();

        // Validate each ingress message in the payload
        for (ingress_id, maybe_ingress) in payload.iter() {
            let ingress = match maybe_ingress {
                Ok(ingress) => ingress,
                Err(deserialization_error) => {
                    return Err(ValidationError::InvalidArtifact(
                        InvalidIngressPayloadReason::IngressMessageDeserializationFailure(
                            ingress_id.clone(),
                            deserialization_error.to_string(),
                        ),
                    ));
                }
            };

            if IngressMessageId::from(&ingress) != *ingress_id {
                return Err(ValidationError::InvalidArtifact(
                    InvalidIngressPayloadReason::MismatchedMessageId {
                        expected: ingress_id.clone(),
                        computed: IngressMessageId::from(&ingress),
                    },
                ));
            }

            self.validate_ingress(
                ingress_id.clone(),
                &ingress,
                &state,
                context,
                &settings,
                &past_ingress,
                0, // message count is checked above.
                &mut cycles_needed,
            )?;
        }
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L564-584)
```rust
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

**File:** rs/replicated_state/src/canister_state/system_state.rs (L945-950)
```rust
    /// Returns the balance after applying the pending 'ingress_induction_cycles_debit'.
    /// Returns 0 if the balance is smaller than the pending 'ingress_induction_cycles_debit'.
    pub fn debited_balance(&self) -> Cycles {
        // We rely on saturating operations of `Cycles` here.
        self.cycles_balance - self.ingress_induction_cycles_debit
    }
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1007-1038)
```rust
    /// Charges the pending 'ingress_induction_cycles_debit' from the balance.
    ///
    /// Precondition:
    /// - The balance is large enough to cover the debit.
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
