Now I have a complete picture of the vulnerability. Let me confirm the exact code path in `validate_and_update_canister_settings`.

### Title
Reserved Cycles Not Returned When Memory Allocation Is Decreased via `update_settings` - (File: `rs/execution_environment/src/canister_manager.rs`)

---

### Summary

When a canister controller calls `update_settings` to **decrease** a canister's `memory_allocation`, the subnet correctly frees the corresponding memory but **never returns the reserved cycles** that were originally paid for that allocation back to the canister's main balance. The reserved cycles remain permanently locked in `reserved_balance`, inaccessible for execution, transfers, or any other use.

---

### Finding Description

The `validate_and_update_canister_settings` function in `rs/execution_environment/src/canister_manager.rs` handles the `update_settings` management canister call. When `memory_allocation` changes, the function computes two quantities:

```rust
let allocated_bytes =
    new_memory_allocation_bytes.saturating_sub(&old_memory_allocation_bytes);
let deallocated_bytes =
    old_memory_allocation_bytes.saturating_sub(&new_memory_allocation_bytes);
``` [1](#0-0) 

When memory allocation is **decreased**, `allocated_bytes` saturates to **zero** and `deallocated_bytes` is positive. The subnet memory is correctly freed:

```rust
} else {
    round_limits.subnet_available_memory.increment(
        deallocated_bytes, NumBytes::from(0), NumBytes::from(0),
    );
}
``` [2](#0-1) 

However, the cycles reservation step that follows uses `allocated_bytes` (which is **zero**):

```rust
let reservation_cycles = self
    .cycles_account_manager
    .storage_reservation_cycles(
        allocated_bytes,          // ← zero when decreasing
        &subnet_memory_saturation,
        subnet_cycles_config,
    )
    .real();
canister
    .system_state
    .reserve_cycles(reservation_cycles)  // ← reserve_cycles(0) is a no-op
    ...
``` [3](#0-2) 

`reserve_cycles` only moves cycles **from** `cycles_balance` **to** `reserved_balance`. There is no inverse `unreserve_cycles` function anywhere in the codebase — a grep for `unreserve_cycles`, `release_reserved`, or `reserved_balance -=` returns zero matches. The `reserve_cycles` implementation confirms the one-way flow:

```rust
pub fn reserve_cycles(&mut self, amount: Cycles) -> Result<(), ReservationError> {
    self.can_reserve_cycles(amount, self.cycles_balance)?;
    self.cycles_balance -= amount;
    self.reserved_balance += amount;
    Ok(())
}
``` [4](#0-3) 

Cycles in `reserved_balance` can only be consumed (burned) by the scheduler for memory/compute/uninstall use cases — they cannot be transferred, used for execution, or otherwise recovered by the canister owner.

---

### Impact Explanation

A canister controller who decreases `memory_allocation` via `update_settings` loses access to the cycles that were originally reserved for the deallocated memory. Those cycles remain locked in `reserved_balance` indefinitely and will eventually be consumed by the scheduler as ongoing storage fees — but at a rate corresponding to the **new, smaller** allocation. The excess reserved cycles (the portion corresponding to the deallocated bytes) are never returned to the main balance and cannot be used for any other purpose. For large memory allocations on a saturated subnet, the reserved cycles can be substantial (on the order of tens of trillions of cycles per GiB).

---

### Likelihood Explanation

Any canister controller can trigger this by calling `update_settings` with a lower `memory_allocation`. This is a standard, documented management canister operation available to all canister controllers. No special privileges, governance majority, or threshold corruption is required. The operation succeeds silently — the controller receives no error and no indication that cycles were not returned.

---

### Recommendation

In the `else` branch of the memory allocation decrease path, compute the cycles corresponding to `deallocated_bytes` at the current subnet memory saturation and move them from `reserved_balance` back to `cycles_balance`. A new `unreserve_cycles(amount: Cycles)` method should be added to `SystemState` that performs the inverse of `reserve_cycles`:

```rust
pub fn unreserve_cycles(&mut self, amount: Cycles) {
    let amount = amount.min(self.reserved_balance);
    self.reserved_balance -= amount;
    self.cycles_balance += amount;
}
```

This should be called in `validate_and_update_canister_settings` when `deallocated_bytes > 0`, analogously to how `reserve_cycles` is called when `allocated_bytes > 0`.

---

### Proof of Concept

1. Deploy a canister on an application subnet with a high subnet memory saturation (above the reservation threshold).
2. Call `update_settings` with `memory_allocation = 2 GiB`. Observe that `reserved_balance` increases by a large amount (e.g., ~100T cycles) and `cycles_balance` decreases by the same amount.
3. Call `update_settings` again with `memory_allocation = 1 GiB` (a 1 GiB decrease).
4. Observe that `subnet_available_memory` increases by 1 GiB (correct), but `reserved_balance` is **unchanged** — the cycles reserved for the deallocated 1 GiB are not returned to `cycles_balance`.
5. The canister controller has permanently lost access to the cycles that were reserved for the 1 GiB that is no longer allocated.

### Citations

**File:** rs/execution_environment/src/canister_manager.rs (L482-485)
```rust
            let allocated_bytes =
                new_memory_allocation_bytes.saturating_sub(&old_memory_allocation_bytes);
            let deallocated_bytes =
                old_memory_allocation_bytes.saturating_sub(&new_memory_allocation_bytes);
```

**File:** rs/execution_environment/src/canister_manager.rs (L502-508)
```rust
            } else {
                round_limits.subnet_available_memory.increment(
                    deallocated_bytes,
                    NumBytes::from(0),
                    NumBytes::from(0),
                );
            }
```

**File:** rs/execution_environment/src/canister_manager.rs (L509-537)
```rust
            let reservation_cycles = self
                .cycles_account_manager
                .storage_reservation_cycles(
                    allocated_bytes,
                    &subnet_memory_saturation,
                    subnet_cycles_config,
                )
                .real();
            canister
                .system_state
                .reserve_cycles(reservation_cycles)
                .map_err(|err| match err {
                    ReservationError::InsufficientCycles {
                        requested,
                        available,
                    } => CanisterManagerError::InsufficientCyclesInMemoryAllocation {
                        memory_allocation: new_memory_allocation,
                        available,
                        threshold: requested,
                    },
                    ReservationError::ReservedLimitExceed { requested, limit } => {
                        CanisterManagerError::ReservedCyclesLimitExceededInMemoryAllocation {
                            memory_allocation: new_memory_allocation,
                            requested,
                            limit,
                        }
                    }
                })?;
            subnet_memory_saturation = subnet_memory_saturation.add(allocated_bytes.get());
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L2097-2101)
```rust
    pub fn reserve_cycles(&mut self, amount: Cycles) -> Result<(), ReservationError> {
        self.can_reserve_cycles(amount, self.cycles_balance)?;
        self.cycles_balance -= amount;
        self.reserved_balance += amount;
        Ok(())
```
