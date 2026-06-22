### Title
Integer Division Truncation in `charge_canister_for_single_resource` Allows Canisters to Avoid Paying Storage/Compute Fees - (File: rs/cycles_account_manager/src/cycles_account_manager.rs)

---

### Summary

The `charge_canister_for_single_resource` function in the IC's `CyclesAccountManager` computes the cycles to charge using integer division that truncates toward zero. For sufficiently small `duration_since_last_charge` values (relative to the per-day rate), the computed charge rounds down to zero cycles. Because the scheduler controls when charging is attempted and the duration is derived from wall-clock time, an attacker who can influence the timing of charge intervals (or who simply operates a canister with very low resource usage) can repeatedly receive zero-cost resource usage across many charge intervals.

---

### Finding Description

In `charge_canister_for_single_resource`:

```rust
let cycles = rate * duration_since_last_charge.as_secs() / SECONDS_PER_DAY;
``` [1](#0-0) 

`SECONDS_PER_DAY` is `86_400` (24 × 60 × 60). [2](#0-1) 

The `rate` is a `CompoundCycles<T>` value computed by `idle_cycles_burned_rate_by_resource`, which itself calls `memory_cost` and `compute_allocation_cost`. Both of those functions already perform integer division internally:

- `memory_cost` divides by `one_gib` (1,073,741,824): [3](#0-2) 

- `scale_cost` divides by `reference_subnet_size`: [4](#0-3) 

After these two truncating divisions, the resulting `rate` value can be very small (e.g., 0 or 1 cycle/day for a canister with minimal memory). When `charge_canister_for_single_resource` then multiplies `rate` by `duration_since_last_charge.as_secs()` and divides by `SECONDS_PER_DAY = 86_400`, the result truncates to **0 cycles** whenever:

```
rate.real() * duration_since_last_charge.as_secs() < 86_400
```

For example, with `rate = 1 cycle/day` and `duration_since_last_charge = 10 seconds` (the configured `duration_between_allocation_charges`):

```
1 * 10 / 86_400 = 0
```

The canister is charged **0 cycles** for that interval. This repeats every charge interval indefinitely.

The scheduler charges canisters every `duration_between_allocation_charges` (10 seconds by default): [5](#0-4) 

And the charge is triggered in `charge_canisters_for_resource_allocation_and_usage`: [6](#0-5) 

The `memory_cost` function for small byte counts already produces 0 cycles before the per-day division even applies:

```
bytes * gib_storage_per_second_fee * duration_secs / one_gib
```

For `bytes < one_gib / (gib_storage_per_second_fee * duration_secs)`, this is 0. With `gib_storage_per_second_fee = 317_500` cycles and `duration_secs = 10`, any canister using fewer than ~337 bytes of memory pays 0 cycles per charge interval. [7](#0-6) 

---

### Impact Explanation

A canister with small but non-zero memory usage (e.g., a few hundred bytes — which every deployed canister has due to canister history) can avoid paying storage fees entirely. The `base_per_second_fee` (`10_000 cycles/second`) is large enough to avoid this for the base fee, but the `gib_storage_per_second_fee` path is vulnerable for small memory footprints. More critically, the **per-day division in `charge_canister_for_single_resource`** means that even a canister with a non-trivial rate (e.g., 8,000 cycles/day) pays 0 cycles per 10-second interval (`8000 * 10 / 86400 = 0`). Over a full day, the canister should pay 8,000 cycles but instead pays 0. This is a **cycles/resource accounting bug** — the protocol systematically undercharges canisters, allowing them to consume subnet resources (memory, compute allocation) without paying the correct fee.

---

### Likelihood Explanation

This affects every canister on every application subnet. The truncation is deterministic and always present — it is not a race condition or edge case. Any canister with a daily resource cost below `86,400 cycles` (i.e., below 1 cycle per second) will never be charged for resource usage. Given that `gib_storage_per_second_fee = 317,500` cycles/GiB/second, a canister using less than ~3.15 KB of memory has a daily cost below 86,400 cycles and pays nothing. This is reachable by any unprivileged canister deployer.

---

### Recommendation

Replace the truncating integer division with ceiling division (round up in the protocol's favor) in `charge_canister_for_single_resource`:

```rust
// Instead of:
let cycles = rate * duration_since_last_charge.as_secs() / SECONDS_PER_DAY;

// Use ceiling division:
let numerator = rate.real() * duration_since_last_charge.as_secs() as u128;
let cycles = (numerator + SECONDS_PER_DAY - 1) / SECONDS_PER_DAY;
```

Similarly, apply ceiling division in `memory_cost` (divide by `one_gib` rounding up) and in `scale_cost` (divide by `reference_subnet_size` rounding up) to prevent compounding truncation losses across the fee calculation chain.

---

### Proof of Concept

Consider a canister with 1,000 bytes of memory on a 13-node application subnet:

1. `gib_storage_per_second_fee = 317,500` cycles/GiB/second
2. `memory_cost(1000 bytes, 10 seconds)`:
   - `(1000 * 317_500 * 10) / 1_073_741_824 = 3_175_000_000 / 1_073_741_824 = 2` cycles (after truncation)
3. `scale_cost(2 cycles, subnet_size=13, reference=13)`:
   - `(2 * 13) / 13 = 2` cycles/day rate
4. In `charge_canister_for_single_resource`:
   - `rate = 2`, `duration = 10 seconds`
   - `2 * 10 / 86_400 = 20 / 86_400 = 0` cycles charged

The canister is charged **0 cycles** every 10 seconds. Over a full day (8,640 charge intervals), it should pay approximately `2 * 86_400 / 86_400 = 2` cycles total, but instead pays **0**. Multiplied across millions of canisters with small memory footprints, this represents a systematic undercollection of fees from the subnet's resource accounting system. [8](#0-7)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L40-41)
```rust
const SECONDS_PER_DAY: u128 = 24 * 60 * 60;
const DAY: Duration = Duration::from_secs(SECONDS_PER_DAY as u64);
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L115-116)
```rust
        let real =
            (cycles * subnet_cycles_config.subnet_size) / self.config.reference_subnet_size.max(1);
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L652-658)
```rust
        let one_gib = 1024 * 1024 * 1024;
        let cycles = Cycles::from(
            (bytes.get() as u128
                * self.config.gib_storage_per_second_fee.get()
                * duration.as_secs() as u128)
                / one_gib,
        );
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1150-1176)
```rust
    fn charge_canister_for_single_resource<T: CyclesUseCaseKind>(
        &self,
        rate: CompoundCycles<T>,
        log: &ReplicaLogger,
        canister: &mut CanisterState,
        duration_since_last_charge: Duration,
    ) -> Result<(), CanisterOutOfCyclesError> {
        let cycles = rate * duration_since_last_charge.as_secs() / SECONDS_PER_DAY;

        // Charging for a resource can charge all the way down to zero cycles.
        if let Err(err) = self.consume_with_threshold(
            &mut canister.system_state,
            cycles,
            Cycles::zero(),
            false, // caller is system => no need to reveal top up balance
        ) {
            info!(
                log,
                "Charging canister {} for {} failed with {}",
                canister.canister_id(),
                T::cycles_use_case().as_str(),
                err
            );
            return Err(err);
        }
        Ok(())
    }
```

**File:** rs/config/src/subnet_config.rs (L517-517)
```rust
            gib_storage_per_second_fee: Cycles::new(317_500),
```

**File:** rs/config/src/subnet_config.rs (L519-519)
```rust
            duration_between_allocation_charges: Duration::from_secs(10),
```

**File:** rs/execution_environment/src/scheduler.rs (L864-874)
```rust
            let duration_since_last_charge =
                canister.duration_since_last_allocation_charge(state_time);
            canister.system_state.time_of_last_allocation_charge = state_time;
            if self
                .cycles_account_manager
                .charge_canister_for_resource_allocation_and_usage(
                    &self.log,
                    canister,
                    duration_since_last_charge,
                    subnet_cycles_config,
                )
```
