Now I have all the information needed for a complete analysis. Let me check the stable memory dirty page limit enforcement to understand the full picture.

### Title
Stable Memory Dirty Pages Excluded from DTS Yield Threshold — Round Monopolization via Stable Memory Writes - (`rs/embedders/src/wasm_executor.rs`)

---

### Summary

The dirty-page yield optimization in `process()` checks only `res.wasm_dirty_pages.len()` to decide whether to yield control back to the replica for a dedicated page-copy slice. Stable memory dirty pages (`res.stable_memory_dirty_pages`) are tracked separately and are never included in this threshold check. A canister that writes exclusively to stable memory can dirty up to 2 GiB of stable memory pages (the per-message limit) in a single execution, bypass the yield optimization entirely, and force all page copying to occur in the same execution round — starving other canisters.

---

### Finding Description

In `rs/embedders/src/wasm_executor.rs`, the yield optimization guard reads:

```rust
let dirty_pages = NumOsPages::from(res.wasm_dirty_pages.len() as u64);
if execution_parameters.instruction_limits.slicing_enabled()
    && dirty_pages.get() > embedder.config().max_dirty_pages_without_optimization as u64
{
    system_api.yield_for_dirty_memory_copy()...
}
``` [1](#0-0) 

`res` is an `InstanceRunResult`, which carries two distinct dirty-page lists:

```rust
pub struct InstanceRunResult {
    pub wasm_dirty_pages: Vec<PageIndex>,
    pub stable_memory_dirty_pages: Vec<PageIndex>,
    pub exported_globals: Vec<Global>,
}
``` [2](#0-1) 

Only `wasm_dirty_pages` feeds the threshold check. `stable_memory_dirty_pages` is populated from the stable bytemap tracker and returned in `InstanceRunResult` at line 1219: [3](#0-2) 

After the yield check (which is never triggered for stable-only writes), the stable memory page copy proceeds unconditionally in the same slice:

```rust
let stable_memory_delta = stable_memory.page_map.update(&compute_page_delta(
    &mut instance,
    &run_result.stable_memory_dirty_pages,
    CanisterMemoryType::Stable,
));
``` [4](#0-3) 

The per-message stable memory dirty page limit is **2 GiB** (524,288 × 4 KiB pages):

```rust
const STABLE_MEMORY_DIRTY_PAGE_LIMIT_MESSAGE: NumOsPages =
    NumOsPages::new(2 * GIB / (PAGE_SIZE as u64));
``` [5](#0-4) 

The yield optimization threshold is **1 GiB** (262,144 × 4 KiB pages):

```rust
pub(crate) const DEFAULT_MAX_DIRTY_PAGES_WITHOUT_OPTIMIZATION: usize = (GIB as usize) / PAGE_SIZE;
``` [6](#0-5) 

The stable memory dirty page limit (2 GiB) is **2× the yield threshold** (1 GiB). A canister writing exclusively to stable memory can dirty up to 2× the threshold without ever triggering the yield optimization.

---

### Impact Explanation

The yield optimization exists precisely to prevent a single canister's page-copy phase from monopolizing an execution round. When a canister writes >1 GiB to stable memory, the yield is never triggered, and the replica must copy up to 2 GiB of dirty stable memory pages synchronously within the same round. This delays all other canisters scheduled in that round. The effect is constrained subnet availability degradation — not a permanent DoS, but a repeatable, attacker-controlled round-time spike.

---

### Likelihood Explanation

Any unprivileged canister controller can deploy a canister and send an update message that calls `stable_write` across >1 GiB of stable memory. No privileged access, governance majority, or threshold corruption is required. The exploit is deterministic and locally testable. The stable memory dirty page limit enforced during execution (via the linker) caps the maximum impact at 2 GiB per message, but that is already above the yield threshold.

---

### Recommendation

Include `stable_memory_dirty_pages` in the dirty-page count used for the yield threshold check:

```rust
let dirty_pages = NumOsPages::from(
    (res.wasm_dirty_pages.len() + res.stable_memory_dirty_pages.len()) as u64
);
```

This ensures that large stable memory writes trigger the same yield optimization as equivalent Wasm heap writes, spreading the page-copy work across a dedicated slice.

---

### Proof of Concept

**Differential test** (mirrors the existing `yield_for_dirty_pages_copy_works` test in `rs/execution_environment/tests/dts.rs`):

1. Deploy two canisters on a DTS-enabled subnet:
   - Canister A: writes >1 GiB to **Wasm heap** (triggers yield → two slices)
   - Canister B: writes >1 GiB to **stable memory** (does not trigger yield → one slice)

2. Send update messages to both simultaneously and observe round execution time.

3. Expected result: Canister B's round takes significantly longer than Canister A's first slice, and other canisters scheduled in the same round as Canister B are delayed.

The existing test at `rs/execution_environment/tests/dts.rs:2717` (`yield_for_dirty_pages_copy_works`) already proves the Wasm heap path works correctly. [7](#0-6) 

An analogous test using `stable_write` instead of `i32.store` would demonstrate that the yield is never triggered for stable memory writes, confirming the gap.

### Citations

**File:** rs/embedders/src/wasm_executor.rs (L681-686)
```rust
    let num_dirty_pages = if let Ok(ref res) = run_result {
        let dirty_pages = NumOsPages::from(res.wasm_dirty_pages.len() as u64);
        // Do not perform this optimization for subnets where DTS is not enabled.
        if execution_parameters.instruction_limits.slicing_enabled()
            && dirty_pages.get() > embedder.config().max_dirty_pages_without_optimization as u64
        {
```

**File:** rs/embedders/src/wasm_executor.rs (L757-761)
```rust
                    let stable_memory_delta = stable_memory.page_map.update(&compute_page_delta(
                        &mut instance,
                        &run_result.stable_memory_dirty_pages,
                        CanisterMemoryType::Stable,
                    ));
```

**File:** rs/embedders/src/lib.rs (L50-54)
```rust
pub struct InstanceRunResult {
    pub wasm_dirty_pages: Vec<PageIndex>,
    pub stable_memory_dirty_pages: Vec<PageIndex>,
    pub exported_globals: Vec<Global>,
}
```

**File:** rs/embedders/src/wasmtime_embedder.rs (L1216-1220)
```rust
            Ok(_) => Ok(InstanceRunResult {
                exported_globals: self.get_exported_globals()?,
                wasm_dirty_pages: access.wasm_dirty_pages,
                stable_memory_dirty_pages: access.stable_dirty_pages,
            }),
```

**File:** rs/config/src/embedders.rs (L84-84)
```rust
pub(crate) const DEFAULT_MAX_DIRTY_PAGES_WITHOUT_OPTIMIZATION: usize = (GIB as usize) / PAGE_SIZE;
```

**File:** rs/config/src/embedders.rs (L100-102)
```rust
// is allowed to produce.
const STABLE_MEMORY_DIRTY_PAGE_LIMIT_MESSAGE: NumOsPages =
    NumOsPages::new(2 * GIB / (PAGE_SIZE as u64));
```

**File:** rs/execution_environment/tests/dts.rs (L2717-2755)
```rust
fn yield_for_dirty_pages_copy_works() {
    let env = ic_state_machine_tests::StateMachineBuilder::new()
        .with_subnet_type(SubnetType::Application)
        .build();

    let wasm = wat::parse_str(WRITE_MORE_THAN_1G_WAT).unwrap();
    let canister_id = env
        .install_canister_with_cycles(wasm, vec![], None, INITIAL_CYCLES_BALANCE)
        .unwrap();

    let mut payload = ic_state_machine_tests::PayloadBuilder::new().with_nonce(0);
    // Send two ingress messages to the same canister.
    for _ in 0..2 {
        payload = payload.ingress(PrincipalId::new_anonymous(), canister_id, "write", vec![]);
    }
    let message_ids = payload.ingress_ids();
    env.execute_payload(payload);

    // Neither of messages should be completed after the first round.
    assert_matches!(
        ingress_state(env.ingress_status(&message_ids[0])),
        Some(IngressState::Processing)
    );
    assert_matches!(
        ingress_state(env.ingress_status(&message_ids[1])),
        Some(IngressState::Received)
    );

    env.tick();

    // Only the first message must be completed after two rounds.
    assert_matches!(
        ingress_state(env.ingress_status(&message_ids[0])),
        Some(IngressState::Completed(_))
    );
    assert_matches!(
        ingress_state(env.ingress_status(&message_ids[1])),
        Some(IngressState::Received)
    );
```
