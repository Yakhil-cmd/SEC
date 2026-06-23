### Title
`delete_canister` Does Not Decrement `compute_allocation_used` in `RoundLimits`, Causing Spurious `SubnetComputeCapacityOverSubscribed` Rejections Within the Same Round - (`rs/execution_environment/src/canister_manager.rs`)

---

### Summary

The `delete_canister` function in `rs/execution_environment/src/canister_manager.rs` correctly frees `round_limits.subnet_available_memory` when a canister is removed, but never decrements `round_limits.compute_allocation_used` by the deleted canister's compute allocation percentage. Because `compute_allocation_used` is the sole gate for accepting new compute-allocation requests within a round, any subsequent `create_canister` or `update_settings` call processed in the same round will see an inflated "used" figure and may be incorrectly rejected with `SubnetComputeCapacityOverSubscribed`, even though real capacity was freed.

---

### Finding Description

`delete_canister` removes the canister from `ReplicatedState` and then updates only the memory dimension of `round_limits`:

```rust
// rs/execution_environment/src/canister_manager.rs  lines 1284-1292
let canister_to_delete = state.remove_canister(&canister_id_to_delete).unwrap();
let canister_memory_allocated_bytes = canister_to_delete.memory_allocated_bytes();

round_limits.subnet_available_memory.increment(
    canister_memory_allocated_bytes,
    NumBytes::from(0),
    NumBytes::from(0),
);
// ← round_limits.compute_allocation_used is never decremented here
``` [1](#0-0) 

The compute-allocation gate used by every subsequent `validate_and_update_canister_settings` call in the same round is:

```rust
// rs/execution_environment/src/canister_manager.rs  lines 412-426
let available_compute_allocation = self
    .config
    .compute_capacity
    .saturating_sub(round_limits.compute_allocation_used)
    .saturating_sub(1)
    .saturating_add(old_compute_allocation.as_percent());
if new_compute_allocation_percent > available_compute_allocation {
    return Err(CanisterManagerError::SubnetComputeCapacityOverSubscribed { … });
}
``` [2](#0-1) 

`round_limits.compute_allocation_used` is initialised once per round from `state.total_compute_allocation()` and then updated incrementally for every allocation change within the round. [3](#0-2) 

`state.total_compute_allocation()` sums allocations over all live canisters, so after the deletion it would return the correct (lower) value — but `round_limits.compute_allocation_used` is never re-read from state mid-round; it is only updated by explicit increments/decrements. Because `delete_canister` performs no decrement, the in-round counter stays inflated by the deleted canister's allocation percentage for the remainder of the round. [4](#0-3) 

By contrast, the analogous memory path is handled correctly: `subnet_available_memory` is incremented on deletion (line 1288) and also incremented inside `uninstall_canister` when `round_limits` is `Some`. [5](#0-4) 

---

### Impact Explanation

Within a single execution round, after a canister with a non-zero `compute_allocation` is deleted, every subsequent `create_canister` or `update_settings` call that requests any compute allocation will compute a falsely low `available_compute_allocation`. If the subnet is near its compute capacity limit, these calls will be rejected with `SubnetComputeCapacityOverSubscribed` even though real capacity was freed. The effect is bounded to one round — at the start of the next round `compute_allocation_used` is re-initialised from the correct `state.total_compute_allocation()` — but within that round legitimate canister operations are denied, constituting a transient resource-accounting denial-of-service.

---

### Likelihood Explanation

The trigger requires two subnet messages to land in the same round: a `delete_canister` for a canister with non-zero compute allocation, followed by a `create_canister` or `update_settings` that requests compute allocation, while the subnet is near its compute capacity. Both messages can be submitted by ordinary canister callers (no privileged role required). On a busy application subnet operating close to the 50 % allocatable compute capacity, this condition can arise naturally or be deliberately arranged by any controller of a stopped canister. [6](#0-5) 

---

### Recommendation

After removing the canister from state, decrement `round_limits.compute_allocation_used` by the deleted canister's compute allocation:

```rust
let deleted_compute_allocation = canister_to_delete
    .system_state
    .compute_allocation
    .as_percent();
round_limits.compute_allocation_used = round_limits
    .compute_allocation_used
    .saturating_sub(deleted_compute_allocation);
```

This mirrors the existing pattern used in `validate_and_update_canister_settings` when compute allocation is reduced. [7](#0-6) 

---

### Proof of Concept

1. Subnet is near compute capacity (e.g., 49 % of 50 % allocatable capacity used).
2. Canister A has `compute_allocation = 10 %` and is stopped.
3. In round R, two subnet messages are queued:
   - Message 1: `delete_canister(A)` — processed first; removes A from state but leaves `compute_allocation_used` at 49 %.
   - Message 2: `create_canister` with `compute_allocation = 5 %` — processed second; computes `available = 50 - 49 - 1 + 0 = 0`, which is less than 5, and returns `SubnetComputeCapacityOverSubscribed`.
4. In round R+1, `compute_allocation_used` is re-initialised to `state.total_compute_allocation()` = 39 %, and the same `create_canister` succeeds.

The discrepancy between round R (spurious rejection) and round R+1 (success) demonstrates the stale counter. [8](#0-7)

### Citations

**File:** rs/execution_environment/src/canister_manager.rs (L405-439)
```rust
        // Compute allocation: validate subnet capacity before the freezing
        // threshold check so that SubnetOversubscribed takes priority.
        if let Some(new_compute_allocation) = settings.compute_allocation {
            // The saturating `u64` subtractions ensure that the available compute
            // capacity of the subnet never goes below zero. This means that even if
            // compute capacity is oversubscribed, the new compute allocation can
            // change between zero and the old compute allocation.
            let available_compute_allocation = self
                .config
                .compute_capacity
                .saturating_sub(round_limits.compute_allocation_used)
                // Minus 1 below guarantees there is always at least 1% of free compute
                // if the subnet was not already oversubscribed.
                .saturating_sub(1)
                .saturating_add(old_compute_allocation.as_percent());
            let old_compute_allocation_percent = old_compute_allocation.as_percent();
            let new_compute_allocation_percent = new_compute_allocation.as_percent();
            if new_compute_allocation_percent > available_compute_allocation {
                return Err(CanisterManagerError::SubnetComputeCapacityOverSubscribed {
                    requested: new_compute_allocation,
                    available: available_compute_allocation,
                });
            }
            if old_compute_allocation_percent < new_compute_allocation_percent {
                round_limits.compute_allocation_used =
                    round_limits.compute_allocation_used.saturating_add(
                        new_compute_allocation_percent - old_compute_allocation_percent,
                    );
            } else {
                round_limits.compute_allocation_used =
                    round_limits.compute_allocation_used.saturating_sub(
                        old_compute_allocation_percent - new_compute_allocation_percent,
                    );
            }
        }
```

**File:** rs/execution_environment/src/canister_manager.rs (L1251-1335)
```rust
    pub(crate) fn delete_canister(
        &self,
        sender: PrincipalId,
        canister_id_to_delete: CanisterId,
        state: &mut ReplicatedState,
        round_limits: &mut RoundLimits,
        subnet_admins: Option<BTreeSet<PrincipalId>>,
    ) -> Result<(), CanisterManagerError> {
        let cost_schedule = state.get_own_cost_schedule();

        if let Ok(canister_id) = CanisterId::try_from(sender)
            && canister_id == canister_id_to_delete
        {
            // A canister cannot delete itself.
            return Err(CanisterManagerError::DeleteCanisterSelf(canister_id));
        }

        let canister_to_delete = self.validate_canister_exists(state, canister_id_to_delete)?;

        validate_controller_or_subnet_admin(canister_to_delete, subnet_admins, &sender)?;

        self.validate_canister_is_stopped(canister_to_delete)?;

        if canister_to_delete.has_input() || canister_to_delete.has_output() {
            return Err(CanisterManagerError::DeleteCanisterQueueNotEmpty(
                canister_id_to_delete,
            ));
        }

        // When a canister is deleted:
        // - its state is permanently deleted, and
        // - its cycles are discarded.

        // Remove the canister from `ReplicatedState`.
        let canister_to_delete = state.remove_canister(&canister_id_to_delete).unwrap();
        let canister_memory_allocated_bytes = canister_to_delete.memory_allocated_bytes();

        round_limits.subnet_available_memory.increment(
            canister_memory_allocated_bytes,
            NumBytes::from(0),
            NumBytes::from(0),
        );

        // Leftover cycles in the canister are considered `consumed`.
        let leftover_cycles = self
            .cycles_account_manager
            .leftover_cycles_for_canister_to_deleted(
                &canister_to_delete.system_state,
                cost_schedule,
            );
        let consumed_cycles_by_canister_to_delete = leftover_cycles.nominal()
            + canister_to_delete
                .system_state
                .canister_metrics()
                .consumed_cycles();

        state
            .metadata
            .subnet_metrics
            .observe_consumed_cycles_with_use_case(
                CyclesUseCase::DeletedCanisters,
                leftover_cycles.nominal(),
            );

        state
            .metadata
            .subnet_metrics
            .observe_consumed_cycles_by_deleted_canisters(consumed_cycles_by_canister_to_delete);

        for (use_case, cycles) in canister_to_delete
            .system_state
            .canister_metrics()
            .consumed_cycles_by_use_cases()
            .iter()
        {
            state
                .metadata
                .subnet_metrics
                .observe_consumed_cycles_with_use_case(*use_case, *cycles);
        }

        // The canister has now been removed from `ReplicatedState` and is dropped
        // once the function is out of scope.
        Ok(())
    }
```

**File:** rs/execution_environment/src/canister_manager.rs (L3079-3086)
```rust
    if let Some(round_limits) = round_limits {
        let deallocated_bytes = old_allocated_bytes.saturating_sub(&new_allocated_bytes);
        round_limits.subnet_available_memory.increment(
            deallocated_bytes,
            NumBytes::from(0),
            NumBytes::from(0),
        );
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L279-300)
```rust
pub struct RoundLimits {
    /// Keeps track of remaining instructions in this execution round.
    pub instructions: RoundInstructions,

    /// Keeps track of the available storage memory. It decreases if
    /// - Wasm execution grows the Wasm/stable memory.
    /// - Wasm execution pushes a new guaranteed response request to the output
    ///   queue.
    pub subnet_available_memory: SubnetAvailableMemory,

    /// The number of outgoing calls that can still be made across the subnet before
    /// canisters are limited to their own callback quota.
    /// This is a soft cap which can be exceeded when executing canisters on threads.
    pub subnet_available_callbacks: i64,

    // TODO would be nice to change that to available, but this requires
    // a lot of changes since available allocation sits in CanisterManager config
    pub compute_allocation_used: u64,

    /// Keeps track of the memory reserved for executing response handlers.
    pub subnet_memory_reservation: NumBytes,
}
```

**File:** rs/replicated_state/src/canister_states.rs (L493-500)
```rust
    pub fn total_compute_allocation(&self) -> u64 {
        let hot: u64 = self
            .hot
            .values()
            .map(|canister| canister.compute_allocation().as_percent())
            .sum();
        hot + self.cold_stats.total_compute_allocation_percent
    }
```
