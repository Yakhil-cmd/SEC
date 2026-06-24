### Title
Flat `base_per_second_fee` Charged Per Canister Regardless of Memory Size Enables Cycles-Cost Pooling via Canister Consolidation - (File: rs/cycles_account_manager/src/cycles_account_manager.rs)

---

### Summary

The IC's `canister_base_cost` function charges a flat `base_per_second_fee` per canister per second, regardless of how much memory that canister holds. This is structurally identical to the `sheepDog` bug: a fixed fee per entity (canister) rather than per unit of resource (byte of memory). An unprivileged canister developer can exploit this by consolidating data from N small canisters into one large canister, reducing their total base fee from `N × base_per_second_fee` to `1 × base_per_second_fee`, while holding the same total amount of data.

---

### Finding Description

In `rs/cycles_account_manager/src/cycles_account_manager.rs`, the function `canister_base_cost` computes the periodic idle storage rent as:

```rust
let cycles = if bytes > NumBytes::new(0) {
    self.config.base_per_second_fee * duration.as_secs() as u128
} else {
    Cycles::zero()
};
```

The fee is `base_per_second_fee × time` — a flat amount that does not scale with `bytes`. The only condition is `bytes > 0`. Whether a canister holds 1 byte or 100 GiB, it pays the same base fee.

The configured value is `10_000` cycles per second per canister on application subnets:

```rust
base_per_second_fee: Cycles::new(10_000),
```

This fee is charged periodically by `charge_canister_for_resource_allocation_and_usage`, which is called by the scheduler's `charge_canisters_for_resource_allocation_and_usage` for every canister on the subnet.

The `idle_cycles_burned_rate_by_resource` function adds this flat base cost on top of the proportional `memory_cost`:

```rust
memory: self.memory_cost(memory, DAY, subnet_cycles_config)
    + self.canister_base_cost(memory, DAY, subnet_cycles_config),
```

So the total daily storage cost for a canister holding `M` bytes is:
`memory_cost(M) + base_per_second_fee × 86400`

For N canisters each holding `M/N` bytes, the total is:
`N × memory_cost(M/N) + N × base_per_second_fee × 86400`

For 1 canister holding `M` bytes, the total is:
`memory_cost(M) + 1 × base_per_second_fee × 86400`

Since `memory_cost` is linear in bytes, `N × memory_cost(M/N) = memory_cost(M)`. The attacker saves `(N-1) × base_per_second_fee × 86400` cycles by consolidating N canisters into one.

---

### Impact Explanation

**Cycles/resource accounting bug.** An unprivileged canister developer who controls N canisters holding the same total data can reduce their ongoing storage rent by a factor of up to N by merging all data into a single canister. At `base_per_second_fee = 10_000` cycles/second and a 13-node subnet (scale factor ≈ 1×), the base fee is `10_000 × 86_400 = 864_000_000` cycles/day per canister. A developer running 1,000 canisters pays `864 × 10^9` cycles/day in base fees alone; by consolidating into one canister they pay `864 × 10^6` cycles/day — a 1000× reduction in base fees. The proportional `gib_storage_per_second_fee` component is unaffected, but the base fee component is entirely avoidable through consolidation. This undercharges large-scale users relative to small-scale users holding the same total data, distorting the economic model of the IC.

---

### Likelihood Explanation

This is trivially exploitable by any canister developer. No special privileges, keys, or subnet-majority corruption are required. Any developer who currently operates multiple canisters storing data can simply migrate their data into fewer canisters. The technique is fully within the normal canister programming model (canisters can store arbitrary data in stable memory). The incentive is real: at scale, the savings in base fees are substantial. Likelihood is **high**.

---

### Recommendation

Change `canister_base_cost` to scale proportionally with memory usage rather than being a flat per-canister fee. For example, fold the base overhead into the `gib_storage_per_second_fee` rate, or make the base fee proportional to `bytes` (e.g., a minimum per-byte rate that applies even at low usage). This mirrors the recommendation in the `sheepDog` report: charge per unit of resource, not per entity holding resources.

---

### Proof of Concept

**Scenario:** Developer controls 100 canisters, each storing 10 MiB of data (total: 1 GiB). Subnet size = 13 (reference size), so scale factor = 1.

**Current cost per day (100 canisters):**
- Memory cost: `(1 GiB × 317_500 × 86_400) / 1 GiB = 27_432_000_000` cycles
- Base fee: `100 × 10_000 × 86_400 = 86_400_000_000` cycles
- **Total: ~113.8 billion cycles/day**

**After consolidation (1 canister, 1 GiB):**
- Memory cost: `27_432_000_000` cycles (unchanged)
- Base fee: `1 × 10_000 × 86_400 = 864_000_000` cycles
- **Total: ~28.3 billion cycles/day**

**Savings: ~85.5 billion cycles/day (~75% reduction)** — achieved by any unprivileged developer via a normal `install_code` + data migration, with no protocol-level barrier.

Root cause: [1](#0-0) 

Fee configuration: [2](#0-1) [3](#0-2) 

Charging path: [4](#0-3) [5](#0-4)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L210-233)
```rust
    fn idle_cycles_burned_rate_by_resource(
        &self,
        memory_allocation: MemoryAllocation,
        memory_usage: NumBytes,
        message_memory_usage: MessageMemoryUsage,
        compute_allocation: ComputeAllocation,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> CyclesBurnedRate {
        let memory = memory_allocation.allocated_bytes(memory_usage);

        CyclesBurnedRate {
            memory: self.memory_cost(memory, DAY, subnet_cycles_config)
                + self.canister_base_cost(memory, DAY, subnet_cycles_config),
            message_memory: self.memory_cost(
                message_memory_usage.total(),
                DAY,
                subnet_cycles_config,
            ),
            compute_allocation: self.compute_allocation_cost(
                compute_allocation,
                DAY,
                subnet_cycles_config,
            ),
        }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L662-674)
```rust
    pub fn canister_base_cost(
        &self,
        bytes: NumBytes,
        duration: Duration,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> CompoundCycles<Memory> {
        let cycles = if bytes > NumBytes::new(0) {
            self.config.base_per_second_fee * duration.as_secs() as u128
        } else {
            Cycles::zero()
        };
        self.scale_cost(cycles, subnet_cycles_config)
    }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1181-1219)
```rust
    pub fn charge_canister_for_resource_allocation_and_usage(
        &self,
        log: &ReplicaLogger,
        canister: &mut CanisterState,
        duration_since_last_charge: Duration,
        subnet_cycles_config: CyclesAccountManagerSubnetConfig,
    ) -> Result<(), CanisterOutOfCyclesError> {
        let CyclesBurnedRate {
            memory,
            message_memory,
            compute_allocation,
        } = self.idle_cycles_burned_rate_by_resource(
            canister.memory_allocation(),
            canister.memory_usage(),
            canister.message_memory_usage(),
            canister.compute_allocation(),
            subnet_cycles_config,
        );

        self.charge_canister_for_single_resource(
            memory,
            log,
            canister,
            duration_since_last_charge,
        )?;
        self.charge_canister_for_single_resource(
            message_memory,
            log,
            canister,
            duration_since_last_charge,
        )?;
        self.charge_canister_for_single_resource(
            compute_allocation,
            log,
            canister,
            duration_since_last_charge,
        )?;

        Ok(())
```

**File:** rs/config/src/subnet_config.rs (L443-444)
```rust
    /// Base fee charged per second for every canister, regardless of resource usage.
    pub base_per_second_fee: Cycles,
```

**File:** rs/config/src/subnet_config.rs (L518-518)
```rust
            base_per_second_fee: Cycles::new(10_000),
```
