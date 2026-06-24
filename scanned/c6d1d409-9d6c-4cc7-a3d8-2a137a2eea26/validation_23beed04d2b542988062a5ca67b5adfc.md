### Title
Unchecked Integer Multiplication in Rate Limiter Capacity Restoration Silently Wraps, Permanently Blocking Legitimate Operations - (File: rs/nervous_system/rate_limits/src/lib.rs)

### Summary
The `update_capacity` function in the nervous system rate limiter library performs an unchecked `u64 * u64` multiplication to compute how much capacity to restore. In Wasm (release mode), integer overflow wraps silently. If the product overflows, the rate limiter subtracts a tiny wrapped value instead of the correct large value, leaving `capacity_used` artificially high and permanently blocking legitimate callers from making reservations.

### Finding Description

In `rs/nervous_system/rate_limits/src/lib.rs`, the `update_capacity` function computes the capacity to restore after elapsed time:

```rust
fn update_capacity(
    usage_record: &mut CapacityUsageRecord,
    now: SystemTime,
    amount_to_add: u64,
    add_frequency: Duration,
) {
    let elapsed = now
        .duration_since(usage_record.last_capacity_drip)
        .unwrap_or(Duration::ZERO);
    let complete_intervals = elapsed.as_secs() / add_frequency.as_secs();  // line 396

    let last_updated = usage_record.last_capacity_drip
        + Duration::from_secs(complete_intervals.saturating_mul(add_frequency.as_secs())); // line 400 — correctly saturating

    // BUG: unchecked multiplication of two u64 values
    let capacity_to_add = complete_intervals * amount_to_add;  // line 403
    usage_record.capacity_used = usage_record.capacity_used.saturating_sub(capacity_to_add);
``` [1](#0-0) 

Line 400 correctly uses `saturating_mul` for the `last_updated` timestamp calculation, but line 403 uses the plain `*` operator for `capacity_to_add`. In Wasm (which wraps on overflow), if `complete_intervals * amount_to_add > u64::MAX`, the product wraps to a small value. The subsequent `saturating_sub` then subtracts that small wrapped value from `capacity_used` instead of the correct large value, leaving `capacity_used` near its maximum and making `get_available_capacity` return near-zero. [2](#0-1) 

This `update_capacity` is called from `with_capacity_usage_record`, which is invoked on every `try_reserve`, `commit`, and `get_available_capacity` call: [3](#0-2) 

The rate limiter is used in production by the registry canister (node provider/operator/IP rate limits) and NNS governance: [4](#0-3) 

### Impact Explanation

If overflow is triggered, `capacity_to_add` wraps to a small value. `capacity_used` is not properly reduced, so `get_available_capacity` returns near-zero. All subsequent `try_reserve` calls for that key return `RateLimiterError::NotEnoughCapacity`, permanently blocking legitimate node provider/operator operations (node additions, subnet admin updates) until the canister is upgraded or the record expires. This is a cycles/resource accounting bug causing availability loss for legitimate governance participants.

### Likelihood Explanation

All currently deployed production configurations use `add_capacity_amount: 1`: [5](#0-4) 

With `amount_to_add = 1`, `complete_intervals * 1` can never overflow. However, the code is structurally incorrect — line 400 uses `saturating_mul` while line 403 uses plain `*` — and any future configuration with `add_capacity_amount > 1` combined with a sufficiently long elapsed time (e.g., a canister that was not called for an extended period) would trigger the overflow. The inconsistency between lines 400 and 403 is a clear oversight. Likelihood is low under current configs but non-zero under configuration drift.

### Recommendation

Replace the unchecked multiplication with `saturating_mul`, consistent with line 400:

```rust
// Before (line 403):
let capacity_to_add = complete_intervals * amount_to_add;

// After:
let capacity_to_add = complete_intervals.saturating_mul(amount_to_add);
``` [6](#0-5) 

### Proof of Concept

1. Configure a `RateLimiter` with `add_capacity_amount = u64::MAX` and `add_capacity_interval = Duration::from_secs(1)`.
2. Commit a reservation to set `capacity_used > 0`.
3. Call `try_reserve` after 2 seconds have elapsed.
4. `complete_intervals = 2`, `amount_to_add = u64::MAX`.
5. `2 * u64::MAX` overflows in Wasm, wrapping to `u64::MAX - 1` (i.e., `2 * 18446744073709551615 mod 2^64 = 18446744073709551614`).
6. `saturating_sub(18446744073709551614)` from any `capacity_used` value yields 0 — in this case the result is actually correct by accident, but with `amount_to_add` values that produce a small wrapped result (e.g., `complete_intervals = 3, amount_to_add = (u64::MAX / 3) + 2`), `capacity_to_add` wraps to a value near 1, leaving `capacity_used` nearly unchanged.
7. `get_available_capacity` returns near-zero; all `try_reserve` calls fail with `NotEnoughCapacity`. [7](#0-6)

### Citations

**File:** rs/nervous_system/rate_limits/src/lib.rs (L334-350)
```rust
    pub fn get_available_capacity(&mut self, key: K, now: SystemTime) -> u64 {
        let committed_capacity =
            self.with_capacity_usage_record(key.clone(), now, |usage| usage.capacity_used);

        let reservations = self.reservations.lock().unwrap();

        // Get all reservations for this key to calculate current usage
        let reserved_capacity: u64 = reservations
            .range((key.clone(), 0)..=(key.clone(), u64::MAX))
            .map(|(_, data)| data.capacity)
            .sum();

        self.config
            .max_capacity
            .saturating_sub(reserved_capacity)
            .saturating_sub(committed_capacity)
    }
```

**File:** rs/nervous_system/rate_limits/src/lib.rs (L353-382)
```rust
    fn with_capacity_usage_record<R>(
        &mut self,
        key: K,
        now: SystemTime,
        f: impl FnOnce(&mut CapacityUsageRecord) -> R,
    ) -> R {
        // Get mutable record
        let mut usage = self
            .capacity_storage
            .remove(&key)
            .unwrap_or(CapacityUsageRecord {
                last_capacity_drip: now,
                capacity_used: 0,
            });

        // Update token bucket capacity so that it's always accurate when retrieved.
        update_capacity(
            &mut usage,
            now,
            self.config.add_capacity_amount,
            self.config.add_capacity_interval,
        );

        let result = f(&mut usage);
        // We only insert the record if there's something in it.
        if usage.capacity_used > 0 {
            self.capacity_storage.upsert(key, usage);
        }
        result
    }
```

**File:** rs/nervous_system/rate_limits/src/lib.rs (L385-409)
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

    // Calculate new last_updated so that the rate remains constant regardless of when this is checked.
    let last_updated = usage_record.last_capacity_drip
        + Duration::from_secs(complete_intervals.saturating_mul(add_frequency.as_secs()));

    // Add capacity for complete intervals (saturating subtract from used capacity)
    let capacity_to_add = complete_intervals * amount_to_add;
    usage_record.capacity_used = usage_record.capacity_used.saturating_sub(capacity_to_add);

    // Set last_updated to account for the remaining partial interval
    // This keeps the partial interval progress for the next call
    usage_record.last_capacity_drip = last_updated;
}
```

**File:** rs/registry/canister/src/rate_limits.rs (L33-69)
```rust
thread_local! {
    static NODE_PROVIDER_RATE_LIMITER: RefCell<
        RateLimiter<String, StableMemoryCapacityStorage<String, VM>>,
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

    /// IP-based rate limiter for add_node operations.
    /// Stored in heap memory (not stable memory).
    /// Limits to 1 node addition per day per IP address.
    static ADD_NODE_IP_RATE_LIMITER: RefCell<InMemoryRateLimiter<String>> =
        RefCell::new(InMemoryRateLimiter::new_in_memory(
            RateLimiterConfig {
                add_capacity_amount: 1,
                add_capacity_interval: Duration::from_secs(ADD_NODE_IP_REFILL_INTERVAL_SECONDS),
                max_capacity: ADD_NODE_IP_MAX_SPIKE,
                max_reservations: ADD_NODE_IP_MAX_SPIKE * 2,
            },
        ));
```
