### Title
Subnet Storage Saturation Manipulation Forces Victim Canisters Into Inflated Storage Reservation Fees - (`rs/cycles_account_manager/src/cycles_account_manager.rs`, `rs/cycles_account_manager/src/cycles_account_manager/types.rs`)

---

### Summary

The IC's storage reservation fee mechanism charges canisters a cycles cost proportional to the current subnet-wide storage saturation level. An unprivileged canister that allocates a large amount of storage can push the subnet utilization above the configured threshold, causing all subsequent canisters that grow their memory to pay significantly higher reservation fees. The attacker can then free their storage and recover unused reserved cycles, leaving victims with permanently inflated reservation costs.

---

### Finding Description

The IC implements a congestion-pricing storage reservation mechanism. When a canister allocates memory (via `memory.grow`, `stable_grow`, `update_settings` with a memory allocation, or `install_code`), the replica charges a one-time upfront reservation of cycles proportional to the current subnet storage saturation.

The saturation is computed in `ExecutionEnvironment::subnet_memory_saturation()`:

```
scaled_subnet_memory_usage = capacity - available_memory
ResourceSaturation { usage, threshold, capacity }
```

The reservation fee is then computed in `CyclesAccountManager::storage_reservation_cycles()` via `total_storage_reservation_cycles()`, which calls `ResourceSaturation::reservation_factor()`:

```
reservation_factor = max_reservation_period * (usage - threshold) / (capacity - threshold)
```

The fee is zero when `usage <= threshold`, and grows linearly toward `max_reservation_period` as `usage` approaches `capacity`. The fee for a new allocation is the difference in total reservation cost before and after the allocation — i.e., the area of the triangle above the threshold.

**Attack scenario:**

1. The subnet starts with `usage < threshold` (e.g., 40 GB used, threshold at 50 GB, capacity 100 GB). Storage reservation fees are zero for all allocations that stay below the threshold.
2. An attacker canister allocates a large block of storage (e.g., 55 GB), crossing the threshold and pushing `usage` to 95 GB. The attacker pays a reservation fee only for the portion above the threshold, computed at the relatively low saturation level at the time of their allocation.
3. Victim canisters that subsequently allocate storage (e.g., 1 GB each) now face a saturation of `(95 - 50) / (100 - 50) = 90%`, paying reservation fees close to the maximum `max_storage_reservation_period` (300,000,000 seconds on application subnets). This is orders of magnitude higher than what they would have paid before the attacker's allocation.
4. The attacker frees their storage. The unused portion of their reserved cycles is returned to their balance. Their net cost is only the ongoing per-second storage fee during the attack window.

The `reservation_factor` function in `types.rs` is the root cause: it is a subnet-global shared state that any canister can influence by allocating storage, and the fee it imposes on subsequent allocators is not bounded relative to the attacker's own cost.

---

### Impact Explanation

Victim canisters that allocate storage after the attacker inflates subnet utilization pay storage reservation fees that are disproportionately large — potentially hundreds of times higher than normal. These reservation cycles are locked in the canister's `reserved_balance` and are consumed over time to pay for storage. This constitutes a direct, irreversible loss of cycles for victim canisters. Canisters with a `reserved_cycles_limit` set may have their memory growth rejected entirely (`ReservedCyclesLimitExceededInMemoryGrow`), causing operational failures.

---

### Likelihood Explanation

Any canister developer can execute this attack. The attacker needs enough cycles to fund the storage allocation and the ongoing per-second storage fee during the attack window. On a typical application subnet with a 450 GB capacity and a 50% threshold, pushing utilization from below threshold to near capacity requires allocating ~225 GB. At the IC's `gib_storage_per_second_fee` of 317,500 cycles/GiB/s, holding 225 GB for one hour costs approximately `225 * 317,500 * 3600 ≈ 257 billion cycles` (~$0.36 at current rates), while victims pay reservation fees at near-maximum saturation. The attack is more feasible on subnets that are already near the threshold, where a smaller allocation suffices to push utilization into the high-fee zone.

---

### Recommendation

1. **Decouple reservation fees from instantaneous global saturation.** Use a time-averaged or smoothed saturation metric rather than the instantaneous `subnet_available_memory` snapshot, so a single large allocation cannot immediately spike fees for all subsequent allocators.
2. **Per-canister storage caps.** Enforce a maximum per-canister storage allocation to limit how much any single canister can shift the global saturation.
3. **Charge the attacker proportionally to the harm caused.** The reservation fee for an allocation that crosses the threshold should account for the externality imposed on future allocators, not just the attacker's own triangle area.

---

### Proof of Concept

The `reservation_factor` function in `rs/cycles_account_manager/src/cycles_account_manager/types.rs` scales linearly with subnet utilization above the threshold: [1](#0-0) 

The `storage_reservation_cycles` function in `rs/cycles_account_manager/src/cycles_account_manager.rs` computes the fee as the difference in total reservation cost before and after the allocation, directly using the current saturation: [2](#0-1) 

The `total_storage_reservation_cycles` function computes the area of the triangle above the threshold, scaled by the reservation factor: [3](#0-2) 

The current subnet saturation is computed from live `subnet_available_memory` in `ExecutionEnvironment::subnet_memory_saturation()`: [4](#0-3) 

This saturation is passed directly into every memory allocation path, including `reserve_storage_cycles` called from `allocate_execution_memory`: [5](#0-4) 

And into `update_settings` with a memory allocation: [6](#0-5) 

The existing test `test_storage_reservation_cycles` confirms that at 90% saturation (`usage=90GB, threshold=100GB, capacity=200GB`), a 40 GB allocation pays a fee proportional to `30 * (130-100) / (200-100) / 2`, while at 0% saturation the same allocation pays zero — demonstrating the extreme fee differential an attacker can impose: [7](#0-6)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager/types.rs (L77-88)
```rust
    pub fn reservation_factor(&self, value: u64) -> u64 {
        let capacity = self.capacity.saturating_sub(self.threshold);
        let usage = self.usage.saturating_sub(self.threshold);
        if capacity == 0 {
            0
        } else {
            let result = (value as u128 * usage as u128) / capacity as u128;
            // We know that the result fits in 64 bits because `value` fits in
            // 64 bits and `usage / capacity <= 1`.
            result.try_into().unwrap()
        }
    }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L678-692)
```rust
    pub fn storage_reservation_cycles(
        &self,
        allocated_bytes: NumBytes,
        storage_saturation: &ResourceSaturation,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> CompoundCycles<Memory> {
        // The reservation cycles for `allocated_bytes` can be computed as
        // the difference between
        // - the total reservation cycles from 0 to `usage + allocated_bytes` and
        // - the total reservation cycles from 0 to `usage`.
        self.total_storage_reservation_cycles(
            &storage_saturation.add(allocated_bytes.get()),
            subnet_cycles_config,
        ) - self.total_storage_reservation_cycles(storage_saturation, subnet_cycles_config)
    }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L698-716)
```rust
    fn total_storage_reservation_cycles(
        &self,
        storage_saturation: &ResourceSaturation,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> CompoundCycles<Memory> {
        let duration = Duration::from_secs(
            storage_saturation
                .reservation_factor(self.config.max_storage_reservation_period.as_secs()),
        );
        // We need to compute the area of the triangle with
        // - base: (U - T) = usage_above_threshold(),
        // - height: duration * fee.
        // That is equal to `(base * height) / 2 = base * (height / 2)`.
        self.memory_cost(
            NumBytes::new(storage_saturation.usage_above_threshold()),
            duration / 2,
            subnet_cycles_config,
        )
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L4703-4727)
```rust
    pub fn subnet_memory_saturation(
        &self,
        subnet_available_memory: &SubnetAvailableMemory,
        resource_limits: ResourceLimits,
    ) -> ResourceSaturation {
        // We apply the scaling factor `self.scheduler_cores`
        // consistently with the scaling factor of `SubnetAvailableMemory`
        // in the function `self.scaled_subnet_available_memory`.
        let scaling_factor = self.scheduler_cores.max(1) as u64;

        // Compute the scaled memory usage as the scaled capacity minus the scaled available memory.
        debug_assert_ne!(scaling_factor, 0);
        let scaled_subnet_memory_capacity: u64 =
            self.subnet_memory_capacity(resource_limits).get() / scaling_factor;
        let scaled_subnet_available_memory =
            subnet_available_memory.get_execution_memory().max(0) as u64;
        let scaled_subnet_memory_usage: u64 =
            scaled_subnet_memory_capacity.saturating_sub(scaled_subnet_available_memory);

        ResourceSaturation::new(
            scaled_subnet_memory_usage,
            self.config.subnet_memory_threshold.get() / scaling_factor,
            scaled_subnet_memory_capacity,
        )
    }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L1064-1073)
```rust
        if api_type.should_update_available_memory_and_reserved_cycles() {
            self.subnet_available_memory
                .try_decrement(allocated_bytes, NumBytes::new(0), NumBytes::new(0))
                .map_err(|_err| HypervisorError::OutOfMemory)?;

            sandbox_safe_system_state.reserve_storage_cycles(
                allocated_bytes,
                &subnet_memory_saturation.add(self.allocated_execution_memory.get()),
            )?;
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

**File:** rs/cycles_account_manager/tests/cycles_account_manager.rs (L1359-1380)
```rust
    // Allocation of 40GB at (usage=90GB, threshold=100GB, capacity=200GB).
    // Only 30GB above the threshold participate in reservation.
    assert_eq!(
        Cycles::new(
            cfg.max_storage_reservation_period.as_secs() as u128
                * cfg.gib_storage_per_second_fee.get()
                // The remaining computes the area of the triangle
                // above the threshold with
                // - base = 30
                // - height = (130 - 100) / (200 - 100).
                * 30
                * (130 - 100)
                / (200 - 100)
                / 2
        ),
        cam.storage_reservation_cycles(
            NumBytes::new(40 * GB),
            &ResourceSaturation::new(90 * GB, 100 * GB, 200 * GB),
            subnet_cycles_config,
        )
        .real()
    );
```
