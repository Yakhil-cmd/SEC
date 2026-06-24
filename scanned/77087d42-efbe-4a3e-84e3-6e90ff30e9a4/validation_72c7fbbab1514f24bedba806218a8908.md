### Title
Query Cache Returns Stale `ic0.canister_liquid_cycle_balance128` Results After Balance Change — (`rs/execution_environment/src/query_handler/query_cache.rs`)

---

### Summary

The IC replica-side query cache uses an `ignore_canister_balances` flag to decide whether a cached query result can be served even after the canister's cycle balance has changed. This flag is computed by checking only `ic0.canister_cycle_balance()` and `ic0.canister_cycle_balance128()` call counters, but **not** `ic0.canister_liquid_cycle_balance128()`. A query that exclusively uses `ic0.canister_liquid_cycle_balance128()` is therefore incorrectly marked as balance-independent, and the cache will serve the stale (pre-balance-change) liquid-balance value to every subsequent caller until the cache entry expires or the canister version changes.

---

### Finding Description

`EntryValue::new()` computes the flag as:

```rust
let ignore_canister_balances = system_api_call_counters.canister_cycle_balance == 0
    && system_api_call_counters.canister_cycle_balance128 == 0;
``` [1](#0-0) 

`canister_liquid_cycle_balance128` is tracked in `SystemApiCallCounters`:

```rust
pub canister_liquid_cycle_balance128: usize,
``` [2](#0-1) 

but is never consulted in the `ignore_canister_balances` decision. The in-code comment in the future-proof guard test explicitly (and incorrectly) states:

> `ic0.canister_cycle_balance[128]()` is the **sole** System API call dependent on canister balance. [3](#0-2) 

`ic0.canister_liquid_cycle_balance128` is implemented as `balance − freeze_threshold`, so it is directly and fully dependent on `cycles_balance`:

```rust
let cycles = self.sandbox_safe_system_state.liquid_cycles_balance(
    self.memory_usage.current_usage,
    self.memory_usage.current_message_usage,
);
``` [4](#0-3) 

`liquid_cycles_balance` itself is:

```rust
pub(super) fn liquid_cycles_balance(...) -> Cycles {
    let cycles = self.cycles_balance();
    let threshold = self.cycles_account_manager.freeze_threshold_cycles(...);
    cycles - threshold
}
``` [5](#0-4) 

The API is available in `NonReplicatedQuery` and `CompositeQuery` contexts (confirmed by the linker registration and the system-API availability matrix), so it is reachable from any unprivileged query caller. [6](#0-5) 

Cache validation in `is_valid` uses the flag as a bypass:

```rust
&& (all_canister_balances_are_valid || self.ignore_canister_balances)
``` [7](#0-6) 

When `ignore_canister_balances` is `true` and the balance has changed, the stale entry is served and the `hits_with_ignored_canister_balance` metric is incremented — but the returned liquid-balance value is wrong.

---

### Impact Explanation

Any canister that exposes a query method returning `ic0.canister_liquid_cycle_balance128()` — without also calling `ic0.canister_cycle_balance()` or `ic0.canister_cycle_balance128()` — will have its query result cached with `ignore_canister_balances = true`. After the canister's cycle balance changes (e.g., due to an update call spending or receiving cycles), every subsequent query call on the same replica will receive the pre-change liquid balance until the cache entry expires or the canister version is bumped. Callers that use the returned value to decide how many cycles to attach to a follow-up inter-canister call, or to gate access to a service, will act on incorrect data. This is the IC analog of the Zetachain pattern: a cached layer (query cache) does not reflect a state change made to the underlying layer (replicated state), and the cached layer's value is served in place of the true current value.

---

### Likelihood Explanation

`ic0.canister_liquid_cycle_balance128` was introduced specifically so canisters can query their *spendable* balance without computing the freeze threshold themselves. It is the natural API for any canister that wants to report or act on its available cycles in a query. Any such canister that does not redundantly also call the deprecated `ic0.canister_cycle_balance` or `ic0.canister_cycle_balance128` will silently trigger the bug. The trigger requires only an unprivileged query call followed by a balance-changing update call followed by another query call — a completely normal usage pattern.

---

### Recommendation

Include `canister_liquid_cycle_balance128` in the `ignore_canister_balances` guard:

```rust
let ignore_canister_balances = system_api_call_counters.canister_cycle_balance == 0
    && system_api_call_counters.canister_cycle_balance128 == 0
    && system_api_call_counters.canister_liquid_cycle_balance128 == 0;
```

Also correct the comment in the future-proof test to list `CanisterLiquidCycleBalance128` as a balance-dependent API that must invalidate cache entries. [8](#0-7) 

---

### Proof of Concept

1. Deploy a canister with:
   ```rust
   #[query]
   fn liquid_balance() -> u128 {
       ic0::canister_liquid_cycle_balance128()  // only this; no canister_cycle_balance call
   }
   ```
2. Call `liquid_balance()` — result `R1` is cached; `ignore_canister_balances = true` because `canister_cycle_balance == 0 && canister_cycle_balance128 == 0`.
3. Execute an update call that burns or receives cycles, changing `cycles_balance`.
4. Call `liquid_balance()` again on the same replica.
5. The cache entry passes `is_valid` (balance changed but `ignore_canister_balances` bypasses the check) and returns `R1` — the pre-change liquid balance — instead of the correct current value.
6. The `hits_with_ignored_canister_balance` metric increments, confirming the stale-hit path was taken. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/execution_environment/src/query_handler/query_cache.rs (L229-241)
```rust
        let includes_data_certificate = system_api_call_counters.data_certificate_copy > 0;
        // It's safe to ignore `batch_time` changes if the query never calls `ic0.time()`.
        let ignore_batch_time = system_api_call_counters.time == 0;
        // It's safe to ignore `canister_balance` changes if the query never checks the balance.
        let ignore_canister_balances = system_api_call_counters.canister_cycle_balance == 0
            && system_api_call_counters.canister_cycle_balance128 == 0;
        EntryValue {
            env,
            result,
            includes_data_certificate,
            ignore_batch_time,
            ignore_canister_balances,
        }
```

**File:** rs/execution_environment/src/query_handler/query_cache.rs (L279-302)
```rust
        if !is_expired
            && !is_expired_data_certificate
            && (self.env.batch_time == now || self.ignore_batch_time)
            && all_canister_versions_are_valid
            && (all_canister_balances_are_valid || self.ignore_canister_balances)
        {
            // The value is still valid.
            metrics.hits.inc();
            // Apply query stats.
            for (id, stats) in canisters_stats {
                // Add query statistics to the query aggregator.
                if let Some(query_stats_collector) = query_stats_collector {
                    query_stats_collector.register_query_statistics(*id, stats);
                }
            }
            // Several factors might cause ignoring behavior simultaneously.
            // To ensure correctness, we need a fallthrough logic here.
            if self.env.batch_time != now && self.ignore_batch_time {
                metrics.hits_with_ignored_time.inc();
            }
            if !all_canister_balances_are_valid && self.ignore_canister_balances {
                metrics.hits_with_ignored_canister_balance.inc();
            }
            true
```

**File:** rs/interfaces/src/execution_environment.rs (L317-317)
```rust
    pub canister_liquid_cycle_balance128: usize,
```

**File:** rs/execution_environment/src/query_handler/query_cache/tests.rs (L1606-1714)
```rust
#[test]
fn query_cache_future_proof_test() {
    match SystemApiCallId::AcceptMessage {
        SystemApiCallId::AcceptMessage
        | SystemApiCallId::CallCyclesAdd
        | SystemApiCallId::CallCyclesAdd128
        | SystemApiCallId::CallDataAppend
        | SystemApiCallId::CallNew
        | SystemApiCallId::CallOnCleanup
        | SystemApiCallId::CallPerform
        | SystemApiCallId::CallWithBestEffortResponse
        | SystemApiCallId::CanisterCycleBalance
        | SystemApiCallId::CanisterCycleBalance128
        | SystemApiCallId::CanisterLiquidCycleBalance128
        | SystemApiCallId::CanisterSelfCopy
        | SystemApiCallId::CanisterSelfSize
        | SystemApiCallId::CanisterStatus
        | SystemApiCallId::CanisterVersion
        | SystemApiCallId::RootKeySize
        | SystemApiCallId::RootKeyCopy
        | SystemApiCallId::CertifiedDataSet
        | SystemApiCallId::CostCall
        | SystemApiCallId::CostCreateCanister
        | SystemApiCallId::CostHttpRequest
        | SystemApiCallId::CostHttpRequestV2
        | SystemApiCallId::CostSignWithEcdsa
        | SystemApiCallId::CostSignWithSchnorr
        | SystemApiCallId::CostVetkdDeriveKey
        | SystemApiCallId::CyclesBurn128
        | SystemApiCallId::DataCertificateCopy
        | SystemApiCallId::DataCertificatePresent
        | SystemApiCallId::DataCertificateSize
        | SystemApiCallId::DebugPrint
        | SystemApiCallId::GlobalTimerSet
        | SystemApiCallId::InReplicatedExecution
        | SystemApiCallId::IsController
        | SystemApiCallId::MintCycles128
        | SystemApiCallId::MsgArgDataCopy
        | SystemApiCallId::MsgArgDataSize
        | SystemApiCallId::MsgCallerCopy
        | SystemApiCallId::MsgCallerInfoDataCopy
        | SystemApiCallId::MsgCallerInfoDataSize
        | SystemApiCallId::MsgCallerInfoSignerCopy
        | SystemApiCallId::MsgCallerInfoSignerSize
        | SystemApiCallId::MsgCallerSize
        | SystemApiCallId::MsgCyclesAccept
        | SystemApiCallId::MsgCyclesAccept128
        | SystemApiCallId::MsgCyclesAvailable
        | SystemApiCallId::MsgCyclesAvailable128
        | SystemApiCallId::MsgCyclesRefunded
        | SystemApiCallId::MsgCyclesRefunded128
        | SystemApiCallId::MsgDeadline
        | SystemApiCallId::MsgMethodNameCopy
        | SystemApiCallId::MsgMethodNameSize
        | SystemApiCallId::MsgReject
        | SystemApiCallId::MsgRejectCode
        | SystemApiCallId::MsgRejectMsgCopy
        | SystemApiCallId::MsgRejectMsgSize
        | SystemApiCallId::MsgReply
        | SystemApiCallId::MsgReplyDataAppend
        | SystemApiCallId::OutOfInstructions
        | SystemApiCallId::PerformanceCounter
        | SystemApiCallId::SubnetSelfSize
        | SystemApiCallId::SubnetSelfCopy
        | SystemApiCallId::Stable64Grow
        | SystemApiCallId::Stable64Read
        | SystemApiCallId::Stable64Size
        | SystemApiCallId::Stable64Write
        | SystemApiCallId::StableGrow
        | SystemApiCallId::StableRead
        | SystemApiCallId::StableSize
        | SystemApiCallId::StableWrite
        | SystemApiCallId::EnvVarCount
        | SystemApiCallId::EnvVarNameSize
        | SystemApiCallId::EnvVarNameCopy
        | SystemApiCallId::EnvVarNameExists
        | SystemApiCallId::EnvVarValueSize
        | SystemApiCallId::EnvVarValueCopy
        | SystemApiCallId::Time
        | SystemApiCallId::Trap
        | SystemApiCallId::TryGrowWasmMemory => {
            ////////////////////////////////////////////////////////////////////
            // ATTENTION!
            ////////////////////////////////////////////////////////////////////
            // By adding a new System API call here, please consider potential
            // direct or indirect effects on the Query Cache.
            //
            // Query Cache coherency relies on three assumptions:
            // * Changes in `batch_time` invalidate cache entries.
            //   `ic0.time()` is the only System API call providing
            //   different values for distinct `batch_time`s.
            // * Changes in `canister_balance` invalidate cache entries.
            //   `ic0.canister_cycle_balance[128]()` is the sole System API
            //   call dependent on canister balance.
            // * Changes in `canister_version` always invalidate cache entries.
            //   This includes update calls, configuration changes, upgrades...
            //
            // If you introduce a new System API call that depends on
            // time or balance or a new Canister property that should
            // invalidate cache entries, please check with the Runtime and/or
            // Execution teams.
            //
            // BREAKING QUERY CACHE COHERENCY CAN RESULT IN UNEXPECTED
            // OUTCOMES. PLEASE DOUBLE-CHECK YOUR DESIGN FOR POTENTIAL
            // QUERY CACHING SIDE EFFECTS.
            ////////////////////////////////////////////////////////////////////
        }
    }
}
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L3600-3604)
```rust
                let cycles = self.sandbox_safe_system_state.liquid_cycles_balance(
                    self.memory_usage.current_usage,
                    self.memory_usage.current_message_usage,
                );
                copy_cycles_to_heap(cycles, dst, heap, method_name)?;
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/sandbox_safe_system_state.rs (L900-916)
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
```

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L895-906)
```rust
    linker
        .func_wrap("ic0", "canister_liquid_cycle_balance128", {
            move |mut caller: Caller<'_, StoreData>, dst: I| {
                let dst: usize = dst.try_into().expect("Failed to convert I to usize");
                charge_for_cpu(&mut caller, overhead::CANISTER_LIQUID_CYCLE_BALANCE128)?;
                with_memory_and_system_api(&mut caller, |s, memory| {
                    s.ic0_canister_liquid_cycle_balance128(dst, memory)
                })?;
                Ok(())
            }
        })
        .unwrap();
```
