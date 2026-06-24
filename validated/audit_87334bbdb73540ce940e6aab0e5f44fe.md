Audit Report

## Title
V128 Straddling Guard Bypassed by All-Zero u128 Group Skip in `stable_dirty_pages_from_bytemap` — (`rs/embedders/src/wasmtime_embedder.rs`)

## Summary

`stable_dirty_pages_from_bytemap` skips entire 16-byte (u128) groups of the stable memory bytemap when all bytes are zero, but does not reset `previous_page_marked_written` nor invoke `handle_bytemap_entry` for the first page of the skipped group. The V128 straddling guard — which detects overflow bytes written by Wasmtime's SIMD-backed `memory.copy` into the page immediately following a written page — lives entirely inside `handle_bytemap_entry` and is therefore silently bypassed. A dirty stable memory page can be omitted from the delta returned to the state manager, causing the certified state hash to diverge from actual in-memory stable memory contents.

## Finding Description

`stable_dirty_pages_from_bytemap` splits the bytemap into `prefix`, `middle`, and `suffix` via `align_to::<u128>()` at line 1289. The middle loop at lines 1303–1316 contains this optimization:

```rust
for group in middle {
    if *group != 0 {
        for (group_index, written) in group.to_ne_bytes().iter().enumerate() {
            handle_bytemap_entry(&mut previous_page_marked_written, ...)?;
        }
    }
    page_index += size_of::<u128>();
}
```

When `*group == 0`, `handle_bytemap_entry` is never called for any of the 16 pages in that group, and `previous_page_marked_written` is never reset to `false`.

The V128 straddling guard at lines 1260–1272 lives entirely inside `handle_bytemap_entry`:

```rust
if *previous_page_marked_written && tracker.is_accessed(index) {
    let first_bytes = &heap_memory[PAGE_SIZE * page_index
        ..PAGE_SIZE * page_index + size_of::<u128>() - 1];
    let previous_bytes = &tracker.get_page(index)[0..size_of::<u128>() - 1];
    if first_bytes != previous_bytes {
        result.push(index);
    }
}
*previous_page_marked_written = false;
```

The injected `StableWrite` replacement (lines 1190–1202 of `system_api_replacements.rs`) uses `MemoryFill` to mark the bytemap for the declared write range, then uses `MemoryCopy` to perform the actual data copy. Wasmtime's `MemoryCopy` implementation uses SIMD/V128 stores on x86-64 — a fact the IC developers explicitly acknowledge in the comment at lines 1261–1265. An unaligned V128 store starting at the last byte of page N writes 15 overflow bytes into page N+1. The bytemap entry for page N+1 remains 0 because `MemoryFill` only covers the declared range.

**Exploit flow**:
1. Canister allocates ≥ 33 pages of stable memory so the bytemap has at least two full u128 groups in `middle`.
2. Canister calls `stable_write(dst = 16 * PAGE_SIZE - 1, src = ..., len = 1)` — writing 1 byte to the last byte of page 15 (last page of the first middle group).
3. `MemoryFill` marks page 15's bytemap entry as dirty; pages 16–31 remain 0.
4. `MemoryCopy` performs a V128 store starting at offset `16 * PAGE_SIZE - 1`, writing 15 overflow bytes into page 16.
5. Page 16 is accessed (SIGSEGV handler fires), so `tracker.is_accessed(page_16)` is `true`.
6. The second middle group (pages 16–31) is all zeros; the loop skips it entirely. `handle_bytemap_entry` is never called for page 16. `previous_page_marked_written` remains `true` but is never acted upon.
7. Page 16 is absent from the returned `stable_memory_dirty_pages` vector (line 1219).
8. `compute_page_delta` (lines 492–514 of `wasm_executor.rs`) builds the stable memory delta from this incomplete list. Page 16's 15 overflow bytes are not included.
9. The stable memory `PageMap` is checkpointed at lines 352–355 of `checkpoint.rs` without page 16's updated contents. The certified state hash is computed from this checkpoint and diverges from actual in-memory stable memory.

## Impact Explanation

The certified state hash does not reflect the actual contents of the canister's stable memory. Any `read_state` query certified against this hash returns stale data for the affected page. This is a concrete certified-state disruption: the state certification invariant is violated for a page whose contents were modified by a V128 overflow that the dirty-page tracking silently missed. This matches the **High** impact category: *"certified-state disruption"* / *"stale certified response accepted under constrained conditions"* ($2,000–$10,000).

## Likelihood Explanation

- No special privileges are required; any canister update call suffices.
- The canister fully controls `dst`, making the alignment condition (write landing at the last byte of a u128-group-boundary page) trivially achievable.
- The zero-group condition is naturally satisfied when the canister writes only to the boundary page and leaves the next 16 pages untouched.
- Wasmtime's use of V128 stores for `memory.copy` on x86-64 is the explicit motivation for the straddling guard already present in the code; the IC developers coded this guard precisely because they know it happens.
- The same class of miss applies at the prefix-to-middle boundary if the last prefix page is marked written and the first middle group is all zeros.

## Recommendation

When `*group == 0` but `previous_page_marked_written` is `true`, still invoke `handle_bytemap_entry` for at least the first page of the group (with `written = 0`) to execute the V128 straddling check, then break early once `previous_page_marked_written` becomes `false`:

```rust
for group in middle {
    if *group != 0 || previous_page_marked_written {
        for (group_index, written) in group.to_ne_bytes().iter().enumerate() {
            handle_bytemap_entry(
                &mut previous_page_marked_written,
                &mut result,
                page_index + group_index,
                heap_memory,
                &tracker,
                *written,
            )?;
            if !previous_page_marked_written && *group == 0 {
                break;
            }
        }
    }
    page_index += size_of::<u128>();
}
```

This ensures the straddling check is always performed for the page immediately following a written page, regardless of whether the containing group is all zeros.

## Proof of Concept

Deploy a canister with ≥ 33 pages of stable memory. In an update call:

1. Call `stable_write(dst = 16 * PAGE_SIZE - 1, src = heap_ptr, len = 1)` with a non-zero byte.
2. After execution, retrieve `stable_memory_dirty_pages` from the `InstanceRunResult`.
3. Assert that page index 15 (the written page) appears in the list — it will.
4. Assert that page index 16 (the V128 overflow target) also appears — it will not, demonstrating the bug.
5. Confirm via direct memory inspection that the first 15 bytes of page 16 in live stable memory differ from the checkpoint on disk, proving the delta is incomplete and the certified state is incorrect.

A deterministic integration test using `PocketIC` or the existing `wasmtime_random_memory_writes` harness (which already exercises `compute_page_delta`) can reproduce this by constructing the exact bytemap layout described above and asserting the presence of page 16 in the dirty list.