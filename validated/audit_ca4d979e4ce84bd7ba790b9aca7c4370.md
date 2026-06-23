### Title
Division-by-Zero Panic in `update_capacity` When `add_capacity_interval` Is Zero — (File: `rs/nervous_system/rate_limits/src/lib.rs`)

---

### Summary

The `RateLimiterConfig` struct in `rs/nervous_system/rate_limits/src/lib.rs` accepts `add_capacity_interval: Duration` with no validation that it is non-zero. The internal `update_capacity` function performs an unchecked integer division by `add_frequency.as_secs()`. If `add_capacity_interval` is `Duration::ZERO`, this is a Rust integer division-by-zero, which panics and traps the canister message. This is the direct IC analog of the Drippie.sol bug: just as `state.config.interval == 0` bypassed rate limiting in Drippie, `add_capacity_interval == Duration::ZERO` in the IC rate limiter library causes every rate-limited operation to trap, permanently disabling the protected functionality (DoS).

---

### Finding Description

In `rs/nervous_system/rate_limits/src/lib.rs`, the `update_capacity` function is called on every invocation of `try_reserve`, `commit`, and `get_available_capacity` via `with_capacity_usage_record`:

```rust
fn update_capacity(
    usage_record: &mut CapacityUsageRecord,
    now: SystemTime,
    amount_to_add: u64,
    add_frequency: Duration,   // ← no non-zero guarantee
) {
    let elapsed = now
        .duration_since(usage_record.last_capacity_drip)
        .unwrap_or(Duration::ZERO);
    // ↓ panics if add_frequency.as_secs() == 0
    let complete_intervals = elapsed.as_secs() / add_frequency.as_secs();
``` [1](#0-0) 

`RateLimiterConfig` is a plain public struct with no constructor validation:

```rust
pub struct RateLimiterConfig {
    pub add_capacity_amount: u64,
    pub add_capacity_interval: Duration,   // ← no > 0 enforcement
    pub max_capacity: u64,
    pub max_reservations: u64,
}
``` [2](#0-1) 

`with_capacity_usage_record` unconditionally calls `update_capacity` with `self.config.add_capacity_interval`: [3](#0-2) 

The library is consumed by production canisters including the NNS governance canister (`new_rate_limiter`), the registry canister node-provider/operator limiters, the node-swap limiter, and the subnet-admins limiter: [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) 

All current callers use hardcoded non-zero constants, so production is not currently affected. However, the library is a shared public API (`ic_nervous_system_rate_limits`) with no enforcement of the invariant `add_capacity_interval > Duration::ZERO`. Any future caller — including a governance-configurable parameter path or a new canister integration — that passes `Duration::ZERO` will trigger the panic on the first rate-limited call.

---

### Impact Explanation

**Vulnerability class:** Cycles/resource accounting bug — the rate limiter is the IC's token-bucket resource accounting primitive used to protect node management, neuron creation, and subnet admin operations.

If `add_capacity_interval` is zero:
- Every call to `try_reserve` → `get_available_capacity` → `with_capacity_usage_record` → `update_capacity` panics with integer division by zero.
- In a canister, a Rust panic becomes a **trap**, causing the update message to be rejected with `CanisterTrapped`.
- The rate-limited operation (e.g., node addition, neuron creation, subnet admin update) becomes **permanently unavailable** for the affected canister — a complete DoS of that functionality.
- Unlike Drippie where interval=0 bypassed the limit (allowing unlimited calls), here interval=0 makes the limit infinitely strict by crashing — the inverse but equally severe outcome.

---

### Likelihood Explanation

**Current production risk:** Low. All existing `RateLimiterConfig` instantiations use hardcoded non-zero `Duration::from_secs(...)` values derived from compile-time constants.

**Future risk:** Medium. The `RateLimiterConfig` struct is a public API with no validation. As the library is adopted by more canisters, or if any interval value becomes governance-configurable (e.g., a proposal to tune rate limits), a zero value — whether by misconfiguration, integer arithmetic underflow in a constant expression, or a governance proposal — would silently compile and then trap at runtime. The Drippie report explicitly noted that JavaScript type coercion could produce an unintended zero; analogously, Rust integer arithmetic (e.g., `ONE_DAY_SECONDS / MAX_OPS` where `MAX_OPS` is a governance-supplied `u64` that could be `u64::MAX` causing the division to round to zero) could produce a zero interval.

---

### Recommendation

1. **Validate at construction time.** Add a `RateLimiterConfig::new(...)` constructor (or a `validate()` method) that returns an error or panics with a clear message if `add_capacity_interval.as_secs() == 0`:

```rust
impl RateLimiterConfig {
    pub fn new(
        add_capacity_amount: u64,
        add_capacity_interval: Duration,
        max_capacity: u64,
        max_reservations: u64,
    ) -> Result<Self, String> {
        if add_capacity_interval.as_secs() == 0 {
            return Err("add_capacity_interval must be > 0 seconds".to_string());
        }
        Ok(Self { add_capacity_amount, add_capacity_interval, max_capacity, max_reservations })
    }
}
```

2. **Guard in `update_capacity`.** As a defense-in-depth measure, add an early return or saturating behavior if `add_frequency.as_secs() == 0`:

```rust
if add_frequency.as_secs() == 0 {
    return; // or treat as "no replenishment"
}
let complete_intervals = elapsed.as_secs() / add_frequency.as_secs();
```

3. **Document the invariant** in `RateLimiterConfig` that `add_capacity_interval` must be at least 1 second, mirroring the Drippie recommendation to document and enforce the interval constraint explicitly.

---

### Proof of Concept

```rust
use ic_nervous_system_rate_limits::{RateLimiter, RateLimiterConfig};
use std::time::{Duration, SystemTime};

fn main() {
    let mut limiter = RateLimiter::new_in_memory(RateLimiterConfig {
        add_capacity_amount: 1,
        add_capacity_interval: Duration::from_secs(0), // ← zero interval, no validation
        max_capacity: 10,
        max_reservations: 100,
    });

    let now = SystemTime::now();
    // Panics: attempt to divide by zero
    // thread 'main' panicked at 'attempt to divide by zero'
    // rs/nervous_system/rate_limits/src/lib.rs:396
    let _ = limiter.try_reserve(now, "key".to_string(), 1);
}
```

The panic occurs at: [8](#0-7) 

In a deployed canister, this becomes a `CanisterTrapped` error on every invocation of the rate-limited endpoint, permanently disabling it.

### Citations

**File:** rs/nervous_system/rate_limits/src/lib.rs (L188-199)
```rust
// Configureation for RateLimiter.
pub struct RateLimiterConfig {
    // How much capacity is restored after each add_capacity_interval.
    pub add_capacity_amount: u64,
    // How frequently capacity is restored after usage.
    pub add_capacity_interval: Duration,
    // Max capacity per item being rate limited.  If there are many items
    // then each would have its own limit, but they would all be max_capacity.
    pub max_capacity: u64,
    // Max reservations across entire space
    pub max_reservations: u64,
}
```

**File:** rs/nervous_system/rate_limits/src/lib.rs (L368-374)
```rust
        // Update token bucket capacity so that it's always accurate when retrieved.
        update_capacity(
            &mut usage,
            now,
            self.config.add_capacity_amount,
            self.config.add_capacity_interval,
        );
```

**File:** rs/nervous_system/rate_limits/src/lib.rs (L385-396)
```rust
fn update_capacity(
    usage_record: &mut CapacityUsageRecord,
    now: SystemTime,
    amount_to_add: u64,
    add_frequency: Duration,
) {
    // Calculate time elapsed since last update
    let elapsed = now
        .duration_since(usage_record.last_capacity_drip)
        .unwrap_or(Duration::ZERO);
    // Calculate how many complete intervals have passed
    let complete_intervals = elapsed.as_secs() / add_frequency.as_secs();
```

**File:** rs/nns/governance/src/governance.rs (L1261-1270)
```rust
fn new_rate_limiter() -> InMemoryRateLimiter<String> {
    RateLimiter::new_in_memory(RateLimiterConfig {
        add_capacity_amount: 1,
        add_capacity_interval: Duration::from_secs(MINIMUM_SECONDS_BETWEEN_ALLOWANCE_INCREASE),
        max_capacity: MAX_NEURON_CREATION_SPIKE,
        // It should not be possible to have more than MAX_NEURON_CREATION_SPIKE_RESERVATIONS
        // because there is only one reservation space being used.
        // But we don't want to hit that error, so we add an extra one.
        max_reservations: MAX_NEURON_CREATION_SPIKE + 1,
    })
```

**File:** rs/registry/canister/src/rate_limits.rs (L36-56)
```rust
    > = RefCell::new(RateLimiter::new_stable(
        RateLimiterConfig {
            add_capacity_amount: 1,
            add_capacity_interval: Duration::from_secs(NODE_PROVIDER_CAPACITY_ADD_INTERVAL_SECONDS),
            max_capacity: NODE_PROVIDER_MAX_SPIKE,
            max_reservations: NODE_PROVIDER_MAX_SPIKE * 2,
        },
        get_node_provider_rate_limiter_memory(),
    ));

    static NODE_OPERATOR_RATE_LIMITER: RefCell<
        RateLimiter<String, StableMemoryCapacityStorage<String, VM>>,
    > = RefCell::new(RateLimiter::new_stable(
        RateLimiterConfig {
            add_capacity_amount: 1,
            add_capacity_interval: Duration::from_secs(NODE_OPERATOR_CAPACITY_ADD_INTERVAL_SECONDS),
            max_capacity: NODE_OPERATOR_MAX_SPIKE,
            max_reservations: NODE_OPERATOR_MAX_SPIKE * 2,
        },
        get_node_operator_rate_limiter_memory(),
    ));
```

**File:** rs/registry/canister/src/mutations/do_swap_node_in_subnet_directly.rs (L44-58)
```rust
    fn new() -> Self {
        Self {
            subnet_limiter: InMemoryRateLimiter::new_in_memory(RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: NODE_SWAPS_SUBNET_CAPACITY_INTERVAL,
                max_capacity: 1,
                max_reservations: 1,
            }),
            node_operator_limiter: InMemoryRateLimiter::new_in_memory(RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: NODE_SWAPS_NODE_OPERATOR_CAPACITY_INTERVAL,
                max_capacity: 1,
                max_reservations: 1,
            }),
        }
```

**File:** rs/registry/canister/src/mutations/do_update_subnet_admins/rate_limits.rs (L20-32)
```rust
impl UpdateSubnetAdminsRateLimiter {
    pub(super) fn new() -> Self {
        Self {
            subnet_limiter: InMemoryRateLimiter::new_in_memory(RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: Duration::from_secs(
                    MINIMUM_SECONDS_BETWEEN_ALLOWANCE_INCREASE,
                ),
                max_capacity: MAX_SUSTAINED_SUBNET_ADMINS_PER_DAY,
                max_reservations: MAX_SUSTAINED_SUBNET_ADMINS_PER_DAY + 1,
            }),
        }
    }
```
