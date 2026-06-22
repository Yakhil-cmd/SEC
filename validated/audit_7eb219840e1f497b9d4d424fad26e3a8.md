### Title
Query Cache Serves Stale Responses for `ic0.canister_liquid_cycle_balance128()` Due to Missing Counter Check in `ignore_canister_balances` Logic — (`rs/execution_environment/src/query_handler/query_cache.rs`)

### Summary

`SystemApiCallCounters` tracks `canister_liquid_cycle_balance128` as a dedicated field, but `EntryValue::new()` computes `ignore_canister_balances` using **only** `canister_cycle_balance` and `canister_cycle_balance128`. A canister that exclusively calls `ic0.canister_liquid_cycle_balance128()` will have its cache entry stored with `ignore_canister_balances = true`, causing the query cache to serve stale responses when the canister's liquid balance changes — even though the response depends on that balance.

---

### Finding Description

`SystemApiCallCounters` has four balance/time-sensitive fields: [1](#0-0) 

The `ignore_canister_balances` flag in `EntryValue::new()` is computed as: [2](#0-1) 

`canister_liquid_cycle_balance128` is **not checked**. If a canister calls only `ic0.canister_liquid_cycle_balance128()` (and never `ic0.canister_cycle_balance()` or `ic0.canister_cycle_balance128()`), the cache stores the entry with `ignore_canister_balances = true`.

The cache validation logic then allows a hit even when the canister's balance has changed: [3](#0-2) 

The `query_cache_future_proof_test` lists `CanisterLiquidCycleBalance128` in its exhaustive match arm: [4](#0-3) 

But the comment inside that arm still claims `ic0.canister_cycle_balance[128]()` is the **sole** balance-dependent API: [5](#0-4) 

This is now incorrect — `ic0.canister_liquid_cycle_balance128()` is also balance-dependent, and the test does not enforce that it is tracked for cache invalidation.

---

### Impact Explanation

A canister whose query method calls only `ic0.canister_liquid_cycle_balance128()` to report or gate on its spendable balance will have its query responses cached with `ignore_canister_balances = true`. When the canister's liquid balance changes (e.g., cycles are received or burned via an update call that does not change `canister_version`, or via subnet-level cycle deductions), the cache will continue serving the old response. Any user or downstream system relying on the query result to reflect the current liquid balance receives stale data.

---

### Likelihood Explanation

`ic0.canister_liquid_cycle_balance128()` is a newly added API. Any canister developer who uses it as the **sole** balance-reading API (a natural choice, since it returns the spendable balance) will silently trigger this bug. The precondition — query caching enabled, canister uses only the new API — is straightforwardly reachable by any unprivileged caller.

---

### Recommendation

Add `canister_liquid_cycle_balance128` to the `ignore_canister_balances` guard in `EntryValue::new()`:

```rust
// rs/execution_environment/src/query_handler/query_cache.rs
let ignore_canister_balances = system_api_call_counters.canister_cycle_balance == 0
    && system_api_call_counters.canister_cycle_balance128 == 0
    && system_api_call_counters.canister_liquid_cycle_balance128 == 0;
```

Also update the comment in `query_cache_future_proof_test` to reflect that `ic0.canister_liquid_cycle_balance128()` is now a balance-dependent API that must be tracked.

---

### Proof of Concept

1. Deploy a canister whose query method calls `ic0.canister_liquid_cycle_balance128()` and returns the result — but never calls `ic0.canister_cycle_balance()` or `ic0.canister_cycle_balance128()`.
2. Issue a query → cache miss; entry stored with `ignore_canister_balances = true` because `canister_cycle_balance == 0 && canister_cycle_balance128 == 0`.
3. Change the canister's cycle balance (e.g., via an update call that deposits or burns cycles, without triggering a `canister_version` increment — or simply wait for subnet-level cycle deductions).
4. Issue the same query → cache **hit** (`ignore_canister_balances = true` bypasses the balance check at line 283), returning the stale liquid balance value.

The root cause is the missing field check at: [6](#0-5)

### Citations

**File:** rs/interfaces/src/execution_environment.rs (L309-320)
```rust
pub struct SystemApiCallCounters {
    /// Counter for `ic0.data_certificate_copy()`
    pub data_certificate_copy: usize,
    /// Counter for `ic0.canister_cycle_balance()`
    pub canister_cycle_balance: usize,
    /// Counter for `ic0.canister_cycle_balance128()`
    pub canister_cycle_balance128: usize,
    /// Counter for `ic0.canister_liquid_cycle_balance128()`
    pub canister_liquid_cycle_balance128: usize,
    /// Counter for `ic0.time()`
    pub time: usize,
}
```

**File:** rs/execution_environment/src/query_handler/query_cache.rs (L232-234)
```rust
        // It's safe to ignore `canister_balance` changes if the query never checks the balance.
        let ignore_canister_balances = system_api_call_counters.canister_cycle_balance == 0
            && system_api_call_counters.canister_cycle_balance128 == 0;
```

**File:** rs/execution_environment/src/query_handler/query_cache.rs (L279-284)
```rust
        if !is_expired
            && !is_expired_data_certificate
            && (self.env.batch_time == now || self.ignore_batch_time)
            && all_canister_versions_are_valid
            && (all_canister_balances_are_valid || self.ignore_canister_balances)
        {
```

**File:** rs/execution_environment/src/query_handler/query_cache/tests.rs (L1617-1620)
```rust
        | SystemApiCallId::CanisterCycleBalance
        | SystemApiCallId::CanisterCycleBalance128
        | SystemApiCallId::CanisterLiquidCycleBalance128
        | SystemApiCallId::CanisterSelfCopy
```

**File:** rs/execution_environment/src/query_handler/query_cache/tests.rs (L1697-1700)
```rust
            // * Changes in `canister_balance` invalidate cache entries.
            //   `ic0.canister_cycle_balance[128]()` is the sole System API
            //   call dependent on canister balance.
            // * Changes in `canister_version` always invalidate cache entries.
```
