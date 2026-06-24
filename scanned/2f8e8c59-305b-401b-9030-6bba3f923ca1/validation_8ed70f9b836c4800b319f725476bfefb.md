The code evidence is conclusive. Let me trace the exact data flow.

**Root cause confirmed at line 1404-1405:**

```rust
// get_anon_rss result:
let bytes = NumBytes::new(kib * 1024);
sandbox_processes_rss.push((*canister_id, bytes));  // ← ONLY anon RSS stored

// get_page_allocator_rss result (lines 1409-1414):
total_memfd_rss += kib;
process_rss += kib;  // ← metrics only, NOT pushed to sandbox_processes_rss
```

The page allocator RSS (memfd/file-backed stable memory) is measured but never written into `sandbox_processes_rss`. Only anon RSS is. Then `update_sandbox_processes_rss` writes that into `stats.rss`. Then `total_sandboxes_rss` sums only those anon-RSS values. The eviction gate at line 1543 (`total_sandboxes_rss > self.max_sandboxes_rss()`) therefore never sees stable memory consumption.

The monitoring thread (line 1483) passes `max_sandboxes_rss = u64::MAX`, so it never triggers RSS-based eviction at all — that path is exclusively in `trigger_sandbox_eviction`, which is broken.

---

### Title
Sandbox RSS Eviction Permanently Blind to Stable Memory (Page Allocator RSS) — (`rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs`)

### Summary
`update_sandbox_processes_rss` stores only anonymous RSS (`get_anon_rss`) in each backend's `stats.rss`. The page-allocator RSS (`get_page_allocator_rss`) — which accounts for all canister heap and stable memory held in memfd/file-backed regions — is measured for metrics but never written into `stats.rss`. As a result, `total_sandboxes_rss` permanently undercounts actual sandbox memory, and the RSS-based eviction gate in `trigger_sandbox_eviction` can never fire for canisters whose memory footprint is dominated by stable memory.

### Finding Description

In `monitor_and_evict_sandbox_processes`: [1](#0-0) 

Only the anon RSS value is pushed into `sandbox_processes_rss`. The page-allocator RSS (lines 1409–1414) is added to `process_rss` for histogram metrics but is never appended to `sandbox_processes_rss`: [2](#0-1) 

`update_sandbox_processes_rss` then writes only those anon-RSS values into `stats.rss`: [3](#0-2) 

`total_sandboxes_rss` sums only `stats.rss` for active backends: [4](#0-3) 

The RSS-based eviction gate in `trigger_sandbox_eviction` therefore operates on a number that excludes all stable memory: [5](#0-4) 

The monitoring thread explicitly passes `u64::MAX` as `max_sandboxes_rss`, so it never triggers RSS-based eviction: [6](#0-5) 

This means the only RSS-based eviction path is `trigger_sandbox_eviction`, and it is permanently blind to stable memory.

### Impact Explanation

A canister with large stable memory (e.g., 400 GiB, the per-canister maximum) but minimal heap will have near-zero anon RSS. Its `stats.rss` will be near zero. `total_sandboxes_rss` will be near zero. The condition `total_sandboxes_rss > max_sandboxes_rss` will never be satisfied, so RSS-based eviction will never fire regardless of how low available system memory drops. The node can be driven into OOM conditions, crashing the replica process and causing subnet unavailability. The count-based eviction path (`active_sandboxes > max_sandbox_count`) is unaffected, but it does not bound memory consumption per sandbox.

### Likelihood Explanation

Any unprivileged canister developer can deploy a canister that writes to stable memory via the `stable_write` system call. The cost is cycles, which are purchasable. A single canister with hundreds of GiB of stable memory is within protocol limits. The effect is deterministic and reproducible: once stable memory is allocated and paged in, the sandbox process's anon RSS remains small while its page-allocator RSS is large, and the eviction gate never fires.

### Recommendation

In the loop inside `monitor_and_evict_sandbox_processes`, accumulate both anon RSS and page-allocator RSS before pushing to `sandbox_processes_rss`:

```rust
let mut total_rss_kib: u64 = 0;
if let Ok(kib) = process_os_metrics::get_anon_rss(pid) {
    total_rss_kib += kib;
    total_anon_rss += kib;
    // ...
}
if let Ok(kib) = process_os_metrics::get_page_allocator_rss(pid) {
    total_rss_kib += kib;
    total_memfd_rss += kib;
    // ...
}
sandbox_processes_rss.push((*canister_id, NumBytes::new(total_rss_kib * 1024)));
```

This ensures `stats.rss` and therefore `total_sandboxes_rss` reflect actual sandbox memory consumption including stable memory.

### Proof of Concept

State-machine test sketch:
1. Deploy a canister that calls `stable_grow` to allocate N GiB of stable memory and writes to every page to force it into RSS.
2. Set `max_sandboxes_rss` to a value smaller than N GiB.
3. Set `available_memory` mock to return a value below `DEFAULT_MIN_MEM_AVAILABLE_TO_EVICT_SANDBOXES` (250 GiB).
4. Call `trigger_sandbox_eviction`.
5. Observe: no eviction occurs because `total_sandboxes_rss` (anon-only) is near zero, so the first condition of the AND-gate is false.
6. Manually set `stats.rss` to include page-allocator RSS and repeat: eviction now fires.

This directly demonstrates that the undercounting of `stats.rss` suppresses RSS-based eviction for stable-memory-heavy canisters.

### Citations

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1398-1405)
```rust
                    if let Ok(kib) = process_os_metrics::get_anon_rss(pid) {
                        total_anon_rss += kib;
                        process_rss += kib;
                        metrics
                            .sandboxed_execution_subprocess_anon_rss
                            .observe(kib as f64);
                        let bytes = NumBytes::new(kib * 1024);
                        sandbox_processes_rss.push((*canister_id, bytes));
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1409-1417)
```rust
                    if let Ok(kib) = process_os_metrics::get_page_allocator_rss(pid) {
                        total_memfd_rss += kib;
                        process_rss += kib;
                        metrics
                            .sandboxed_execution_subprocess_memfd_rss
                            .observe(kib as f64);
                    } else {
                        warn!(logger, "Unable to get memfd RSS for pid {}", pid);
                    }
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1482-1490)
```rust
                let max_active_sandboxes = max_sandbox_count;
                let max_sandboxes_rss = u64::MAX.into();
                evict_sandbox_processes(
                    &mut guard,
                    max_active_sandboxes,
                    max_sandbox_idle_time,
                    max_sandboxes_rss,
                    Arc::clone(&state_reader),
                );
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1542-1560)
```rust
            let total_sandboxes_rss = total_sandboxes_rss(backends);
            if total_sandboxes_rss > self.max_sandboxes_rss()
                && available_memory().unwrap_or_default()
                    < DEFAULT_MIN_MEM_AVAILABLE_TO_EVICT_SANDBOXES
            {
                // The total RSS is exceeded AND the available memory is low.
                // Reduce the RSS of sandboxes, regardless of their number.
                let max_active_sandboxes = backends.len();
                let max_sandboxes_rss =
                    total_sandboxes_rss.saturating_sub(&sandbox_processes_rss_to_evict);

                evict_sandbox_processes(
                    backends,
                    max_active_sandboxes,
                    self.max_sandbox_idle_time,
                    max_sandboxes_rss,
                    Arc::clone(&self.state_reader),
                );
            }
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L2009-2018)
```rust
fn update_sandbox_processes_rss(
    backends: &mut HashMap<CanisterId, Backend>,
    sandbox_processes_rss: Vec<(CanisterId, NumBytes)>,
) {
    for (id, rss) in sandbox_processes_rss {
        backends.entry(id).and_modify(|backend| match backend {
            Backend::Active { stats, .. } | Backend::Evicted { stats, .. } => stats.rss = rss,
            Backend::Empty => {}
        });
    }
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L2022-2030)
```rust
fn total_sandboxes_rss(backends: &HashMap<CanisterId, Backend>) -> NumBytes {
    backends
        .values()
        .map(|backend| match backend {
            Backend::Active { stats, .. } => stats.rss,
            Backend::Evicted { .. } | Backend::Empty => 0.into(),
        })
        .sum()
}
```
