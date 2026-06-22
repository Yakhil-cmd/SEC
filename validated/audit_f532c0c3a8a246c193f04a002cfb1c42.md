### Title
Query Cache Serves Stale Responses for `ic0_canister_liquid_cycle_balance128` Due to Missing Counter Check in `ignore_canister_balances` — (`rs/execution_environment/src/query_handler/query_cache.rs`)

### Summary

`ic0_canister_liquid_cycle_balance128` is tracked in `SystemApiCallCounters` but its counter is **not consulted** when computing `ignore_canister_balances` in `EntryValue::new()`. A canister that uses only this API (not `ic0_canister_cycle_balance` or `ic0_canister_cycle_balance128`) will have `ignore_canister_balances = true` set on its cache entry, causing the query cache to serve stale liquid-balance data after the canister's balance changes.

### Finding Description

`SystemApiCallCounters` has four balance/time-related fields: [1](#0-0) 

The `ignore_canister_balances` flag is computed as:

```rust
let ignore_canister_balances = system_api_call_counters.canister_cycle_balance == 0
    && system_api_call_counters.canister_cycle_balance128 == 0;
``` [2](#0-1) 

The `canister_liquid_cycle_balance128` field is **never read** in this computation. A canister that calls only `ic0_canister_liquid_cycle_balance128` will have `canister_cycle_balance == 0` and `canister_cycle_balance128 == 0`, so `ignore_canister_balances` is set to `true`. The cache then skips balance-change invalidation: [3](#0-2) 

`ic0_canister_liquid_cycle_balance128` returns the spendable balance (total minus freeze threshold), which is directly balance-dependent: [4](#0-3) 

The `query_cache_future_proof_test` lists `CanisterLiquidCycleBalance128` in its exhaustive match but only provides a comment warning — it does **not** enforce that the counter is checked: [5](#0-4) 

### Impact Explanation

A canister whose query method calls only `ic0_canister_liquid_cycle_balance128` will have its response cached with `ignore_canister_balances = true`. After the canister's balance changes (cycles received, burned, or freeze threshold updated), subsequent queries receive the stale cached liquid-balance value until the canister version changes or the cache entry expires. This breaks the stated query cache coherency invariant.

**Note on scope**: Query responses are not certified by default (only `data_certificate_copy` produces certified data). The impact is stale non-certified query responses, not stale certified state. The `ic0_env_var_value_copy` case raised in the question is **not** a vulnerability — environment variable changes go through `update_settings`, which bumps `canister_version`, and version changes always invalidate cache entries unconditionally. [6](#0-5) 

### Likelihood Explanation

Any canister that uses `ic0_canister_liquid_cycle_balance128` exclusively (without also calling `ic0_canister_cycle_balance` or `ic0_canister_cycle_balance128`) is affected. The universal canister already exposes this API: [7](#0-6) 

An unprivileged user can trigger this by querying such a canister after its balance changes.

### Recommendation

Add `canister_liquid_cycle_balance128` to the `ignore_canister_balances` guard in `EntryValue::new()`:

```rust
let ignore_canister_balances = system_api_call_counters.canister_cycle_balance == 0
    && system_api_call_counters.canister_cycle_balance128 == 0
    && system_api_call_counters.canister_liquid_cycle_balance128 == 0;
``` [2](#0-1) 

Also add a corresponding test analogous to `query_cache_returns_different_results_for_different_canister_balance128s` for the liquid balance variant.

### Proof of Concept

1. Deploy a canister with a query method that calls `ic0_canister_liquid_cycle_balance128` and returns the result.
2. Issue query → cache miss; entry stored with `ignore_canister_balances = true` (since `canister_cycle_balance == 0 && canister_cycle_balance128 == 0`).
3. Change the canister's cycle balance (e.g., burn cycles via an update call — this does NOT bump canister version if done via `cycles_burn128` in a query context, or via direct balance manipulation in tests).
4. Issue the same query → cache **hit** returning the old stale liquid balance.

The counter is incremented correctly: [8](#0-7) 

But the `ignore_canister_balances` computation never reads `system_api_call_counters.canister_liquid_cycle_balance128`, so the stale-serving path is taken.

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

**File:** rs/execution_environment/src/query_handler/query_cache.rs (L264-265)
```rust
            if &canister.system_state.canister_version() != version {
                all_canister_versions_are_valid = false;
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

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L3577-3607)
```rust
    fn ic0_canister_liquid_cycle_balance128(
        &mut self,
        dst: usize,
        heap: &mut [u8],
    ) -> HypervisorResult<()> {
        self.call_counters.canister_liquid_cycle_balance128 += 1;
        let method_name = "ic0_canister_liquid_cycle_balance128";
        let result = match &self.api_type {
            ApiType::Start { .. } => Err(self.error_for(method_name)),
            ApiType::Init { .. }
            | ApiType::SystemTask { .. }
            | ApiType::Update { .. }
            | ApiType::Cleanup { .. }
            | ApiType::CompositeCleanup { .. }
            | ApiType::ReplicatedQuery { .. }
            | ApiType::NonReplicatedQuery { .. }
            | ApiType::CompositeQuery { .. }
            | ApiType::PreUpgrade { .. }
            | ApiType::ReplyCallback { .. }
            | ApiType::CompositeReplyCallback { .. }
            | ApiType::RejectCallback { .. }
            | ApiType::CompositeRejectCallback { .. }
            | ApiType::InspectMessage { .. } => {
                let cycles = self.sandbox_safe_system_state.liquid_cycles_balance(
                    self.memory_usage.current_usage,
                    self.memory_usage.current_message_usage,
                );
                copy_cycles_to_heap(cycles, dst, heap, method_name)?;
                Ok(())
            }
        };
```

**File:** rs/execution_environment/src/query_handler/query_cache/tests.rs (L1619-1619)
```rust
        | SystemApiCallId::CanisterLiquidCycleBalance128
```

**File:** rs/universal_canister/impl/src/api.rs (L339-343)
```rust
pub fn liquid_balance128() -> Vec<u8> {
    let mut bytes = vec![0_u8; CYCLES_SIZE];
    unsafe { ic0::canister_liquid_cycle_balance128(bytes.as_mut_ptr() as u32) }
    bytes
}
```
