### Title
Query Cache Incoherence: `ic0.canister_liquid_cycle_balance128()` Calls Not Tracked for Cache Invalidation, Causing Stale Balance Results - (File: `rs/execution_environment/src/query_handler/query_cache.rs`)

---

### Summary

The replica-side query cache determines whether a cached entry can be reused when the canister's cycle balance changes by checking whether the query called `ic0.canister_cycle_balance()` or `ic0.canister_cycle_balance128()`. However, the newer `ic0.canister_liquid_cycle_balance128()` System API call — which also depends on the canister's balance — is **not included** in this check. As a result, any query that exclusively uses `ic0.canister_liquid_cycle_balance128()` to read the canister's spendable balance will have its result cached with `ignore_canister_balances = true`, causing the cache to serve stale liquid-balance data to subsequent callers even after the canister's balance has materially changed.

---

### Finding Description

The query cache in `rs/execution_environment/src/query_handler/query_cache.rs` uses `SystemApiCallCounters` to decide whether a cached entry is still valid when the canister's cycle balance changes. The decision is made in `EntryValue::new`:

```rust
// It's safe to ignore `canister_balance` changes if the query never checks the balance.
let ignore_canister_balances = system_api_call_counters.canister_cycle_balance == 0
    && system_api_call_counters.canister_cycle_balance128 == 0;
``` [1](#0-0) 

The `SystemApiCallCounters` struct tracks a **third** balance-related call:

```rust
pub struct SystemApiCallCounters {
    pub canister_cycle_balance: usize,
    pub canister_cycle_balance128: usize,
    pub canister_liquid_cycle_balance128: usize,   // tracked but never consulted
    pub time: usize,
    ...
}
``` [2](#0-1) 

The implementation of `ic0_canister_liquid_cycle_balance128` correctly increments `call_counters.canister_liquid_cycle_balance128`:

```rust
fn ic0_canister_liquid_cycle_balance128(...) {
    self.call_counters.canister_liquid_cycle_balance128 += 1;
    ...
    let cycles = self.sandbox_safe_system_state.liquid_cycles_balance(...);
    ...
}
``` [3](#0-2) 

The liquid balance is computed as `total_balance − freeze_threshold`, where the freeze threshold depends on memory usage, compute allocation, and subnet configuration:

```rust
pub(super) fn liquid_cycles_balance(...) -> Cycles {
    let cycles = self.cycles_balance();
    let threshold = self.cycles_account_manager.freeze_threshold_cycles(...);
    cycles - threshold
}
``` [4](#0-3) 

Because `canister_liquid_cycle_balance128` is never consulted in the `ignore_canister_balances` computation, a query that **only** calls `ic0.canister_liquid_cycle_balance128()` — and never calls `ic0.canister_cycle_balance[128]()` — will be cached with `ignore_canister_balances = true`. The cache's `is_valid` check will then skip balance invalidation for that entry:

```rust
&& (all_canister_balances_are_valid || self.ignore_canister_balances)
``` [5](#0-4) 

The in-code comment in the test suite even documents the incorrect assumption:

> `ic0.canister_cycle_balance[128]()` is the **sole** System API call dependent on canister balance. [6](#0-5) 

This assumption is false: `ic0.canister_liquid_cycle_balance128()` is equally balance-dependent.

---

### Impact Explanation

Any canister that exposes a query method using `ic0.canister_liquid_cycle_balance128()` to report its spendable balance will serve **stale cached results** to callers after its balance changes. Concretely:

- **Off-chain monitoring or automation** (e.g., a cycles top-up bot) that polls a canister's liquid balance via query will receive an outdated value, potentially failing to top up a canister that is about to be frozen, or topping up one that no longer needs it.
- **Composite queries** that branch on the liquid balance (e.g., "do I have enough liquid cycles to proceed?") will receive the cached stale answer, causing incorrect control flow.
- **Canister-level financial logic** exposed via query (e.g., a DeFi canister reporting available liquidity) will return manipulable stale data to callers.

The stale result persists until the cache entry expires (`max_expiry_time`) or the canister version changes (e.g., via an upgrade or update call). During that window, every query caller receives the incorrect liquid balance.

---

### Likelihood Explanation

- `ic0.canister_liquid_cycle_balance128()` is a production System API available to all canisters in `NonReplicatedQuery` and `CompositeQuery` contexts.
- Any unprivileged query caller can trigger the bug by simply sending a query to a canister that uses this API.
- The balance of a canister can change between queries through normal protocol operation (cycles consumed by heartbeats, timers, or incoming calls; cycles sent by other canisters).
- No special privileges, governance majority, or threshold corruption are required.

---

### Recommendation

Include `canister_liquid_cycle_balance128` in the `ignore_canister_balances` guard:

```rust
let ignore_canister_balances = system_api_call_counters.canister_cycle_balance == 0
    && system_api_call_counters.canister_cycle_balance128 == 0
    && system_api_call_counters.canister_liquid_cycle_balance128 == 0;
```

Additionally, note that the liquid balance also depends on **memory usage** (via the freeze threshold), which is not tracked by the cache at all. A complete fix should also consider invalidating cache entries when the canister's memory footprint changes, or document that `ic0.canister_liquid_cycle_balance128()` results may be stale due to memory-usage-driven threshold changes even after the above fix.

---

### Proof of Concept

1. Deploy a canister `C` with a query method `get_liquid_balance` that calls `ic0.canister_liquid_cycle_balance128()` and returns the result. Crucially, it does **not** call `ic0.canister_cycle_balance()` or `ic0.canister_cycle_balance128()`.

2. Send a query `get_liquid_balance` to `C`. The replica executes the query, records `canister_liquid_cycle_balance128 = 1`, but sets `ignore_canister_balances = true` (because `canister_cycle_balance == 0` and `canister_cycle_balance128 == 0`). The result — say, `1_000_000` cycles — is cached.

3. Send cycles to `C` (or have `C`'s heartbeat consume cycles), changing its balance. The cache entry is **not** invalidated because `ignore_canister_balances = true` causes the balance check to be skipped in `is_valid`.

4. Send the same query `get_liquid_balance` again. The cache returns the stale `1_000_000` value instead of the updated liquid balance.

5. An off-chain system relying on this query result makes incorrect decisions (e.g., does not top up `C` because it appears to have sufficient liquid cycles, while `C` is actually near the freeze threshold).

### Citations

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

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L3577-3615)
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
        trace_syscall!(
            self,
            CanisterLiquidCycleBalance128,
            dst,
            summarize(heap, dst, 16)
        );
        result
    }
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L900-917)
```rust
    pub(super) fn liquid_cycles_balance(
        &self,
        current_memory_usage: NumBytes,
        current_message_memory_usage: MessageMemoryUsage,
    ) -> Cycles {
        let cycles = self.cycles_balance();
        let threshold = self.cycles_account_manager.freeze_threshold_cycles(
            self.freeze_threshold,
            self.memory_allocation,
            current_memory_usage,
            current_message_memory_usage,
            self.compute_allocation,
            self.subnet_cycles_config,
            self.reserved_balance(),
        );
        // Here we rely on the saturating subtraction for Cycles.
        cycles - threshold
    }
```

**File:** rs/execution_environment/src/query_handler/query_cache/tests.rs (L1693-1699)
```rust
            // Query Cache coherency relies on three assumptions:
            // * Changes in `batch_time` invalidate cache entries.
            //   `ic0.time()` is the only System API call providing
            //   different values for distinct `batch_time`s.
            // * Changes in `canister_balance` invalidate cache entries.
            //   `ic0.canister_cycle_balance[128]()` is the sole System API
            //   call dependent on canister balance.
```
