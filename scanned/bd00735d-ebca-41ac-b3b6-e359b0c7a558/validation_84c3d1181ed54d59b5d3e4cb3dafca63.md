### Title
Compromised Sandbox Can Crash Replica via Unvalidated `file_offset` in `deserialize_page` — (`rs/replicated_state/src/page_map/page_allocator/mmap.rs`)

---

### Summary

`MmapBasedPageAllocatorCore::deserialize_page` contains an `assert!` that verifies a page fits entirely within a mapped chunk. This assertion is reachable with attacker-controlled data from the sandbox IPC channel, and no validation of `file_offset` is performed before the assertion is evaluated. A compromised sandbox process can send a `PageDeltaSerialization` with a non-page-aligned `file_offset` (e.g., `file_len - 1`) to trigger the assertion, panicking the replica process.

---

### Finding Description

`deserialize_page` iterates over chunks to find the one containing `file_offset`. When a chunk is found (i.e., `chunk.offset <= file_offset < chunk.offset + chunk.size`), it asserts the full page fits within the chunk: [1](#0-0) 

The outer `if` condition only checks that the **start** of the page is within the chunk. The inner `assert!` then checks that the **end** of the page is also within the chunk. If `file_offset = chunk.offset + chunk.size - 1`, the outer condition is satisfied but the inner assertion evaluates to:

```
(chunk.offset + chunk.size - 1) + 4096 <= (chunk.offset + chunk.size)
=> chunk.offset + chunk.size + 4095 <= chunk.offset + chunk.size
=> false  →  PANIC
```

The caller `deserialize_page_delta` performs **no validation** of `file_offset` values before calling `deserialize_page`. The comment at line 301 states the precondition as an assumption, not an enforced check: [2](#0-1) 

The `PageDeltaSerialization` (including `file_offset` in each `MmapPageSerialization`) is deserialized directly from the sandbox IPC channel: [3](#0-2) 

The replica's `update_execution_state` calls `deserialize_delta` on both wasm and stable memory page maps with no intervening validation of offsets: [4](#0-3) 

The IC threat model explicitly acknowledges that the sandbox may be compromised — the instruction count is clamped defensively — but no equivalent guard exists for `file_offset`: [5](#0-4) 

There is no `catch_unwind` wrapping `process_completion` or `update_execution_state`, so the `assert!` panic propagates uncaught and terminates the replica process.

---

### Impact Explanation

A compromised sandbox process can crash the replica node by sending a single malformed `PageDeltaSerialization` with `file_offset = file_len - 1` (or any non-page-aligned offset within a chunk). This is a **replica process crash / denial of service**. Because each canister has its own sandbox process, a single malicious canister that achieves sandbox escape can crash the entire replica node, taking down all canisters on that node.

---

### Likelihood Explanation

The precondition is a compromised sandbox process. This requires a separate exploit (e.g., a Wasm JIT/interpreter vulnerability allowing sandbox escape). However, the IC security model explicitly treats sandbox compromise as a realistic threat and defends against it in other places (instruction count clamping). The missing `file_offset` validation is an oversight in an otherwise intentional defense-in-depth layer. The exploit itself — once the sandbox is compromised — is trivial: send one IPC message with `file_offset = file_len - 1`.

---

### Recommendation

In `deserialize_page_delta`, validate each `file_offset` before calling `deserialize_page`:

1. Assert `file_offset` is page-aligned: `file_offset % PAGE_SIZE == 0`
2. Assert `file_offset + PAGE_SIZE <= file_len`: the full page lies within the declared file length

These checks should return an error (or log and skip) rather than panic, consistent with the defensive handling of the instruction count. The `assert!` inside `deserialize_page` can remain as a debug-only invariant check (`debug_assert!`) once the input is validated at the boundary.

---

### Proof of Concept

```rust
// In deserialize_page_delta, before calling deserialize_page:
// Attacker-controlled PageDeltaSerialization:
//   file_len = 4096
//   pages = [MmapPageSerialization { page_index: 0, file_offset: 4095, validation: ... }]
//
// After grow_for_deserialization(4096):
//   chunk = Chunk { offset: 0, size: 4096 }
//
// In deserialize_page with file_offset = 4095:
//   chunk.offset(0) <= 4095 < chunk.offset + chunk.size(4096)  → true (chunk found)
//   assert!(4095 + 4096 <= 4096)  →  assert!(8191 <= 4096)  →  PANIC
``` [6](#0-5) [7](#0-6)

### Citations

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L291-312)
```rust
    pub fn deserialize_page_delta(
        page_allocator: &Arc<PageAllocatorInner>,
        page_delta: PageDeltaSerialization,
    ) -> Vec<(PageIndex, Page)> {
        let mut guard = page_allocator.core_allocator.lock().unwrap();
        let core = guard.as_mut().unwrap();
        core.grow_for_deserialization(page_delta.file_len);
        core.deserialized_pages += page_delta.pages.len();
        // Deserialized pages are considered as allocated for the purposes of the metric.
        ALLOCATED_PAGES.inc_by(page_delta.pages.len());
        // File offsets of all pages are smaller than `file_len`, which means
        // that the precondition of `deserialize_page()` is fulfilled after
        // the call to `grow_for_deserialization(file_len)`.
        page_delta
            .pages
            .into_iter()
            .map(|ser| {
                let page = core.deserialize_page(&ser, page_allocator);
                (ser.page_index, Page(Arc::new(page)))
            })
            .collect()
    }
```

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L667-715)
```rust
    fn grow_for_deserialization(&mut self, file_len: FileOffset) {
        if file_len == self.file_len {
            return;
        }
        if file_len < self.file_len {
            // This may happen if another thread already called `grow_for_deserialization`
            // while this thread was waiting for the lock. In that case the actual file
            // length is the same or is larger than the saved file length.
            let actual_file_len = unsafe { get_file_length(self.file_descriptor) };
            assert!(
                actual_file_len >= self.file_len,
                "The page allocator file was truncated: actual file_len = {}, new file_len = {}, old file_len = {}",
                actual_file_len,
                file_len,
                self.file_len
            );
            return;
        }
        let mmap_size = (file_len - self.file_len) as usize;
        let mmap_file_offset = self.file_len;
        self.file_len = file_len;

        // The mapping is read/write because freeing of pages uses `madvise()` with
        // `MADV_REMOVE`, which requires writable mapping.
        // SAFETY: The parameters are valid.
        let mmap_ptr = unsafe {
            mmap(
                std::ptr::null_mut(),
                mmap_size,
                ProtFlags::PROT_READ | ProtFlags::PROT_WRITE,
                MapFlags::MAP_SHARED,
                self.file_descriptor,
                mmap_file_offset,
            )
        }
        .unwrap_or_else(|err| {
            panic!(
                "MmapPageAllocator failed to mmap {} bytes to memory file #{} \
                         at offset {} for deserialization: {}",
                mmap_size, self.file_descriptor, mmap_file_offset, err,
            )
        }) as *mut u8;

        self.chunks.push(Chunk {
            ptr: mmap_ptr,
            size: mmap_size,
            offset: mmap_file_offset,
        });
    }
```

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L720-761)
```rust
    fn deserialize_page(
        &self,
        serialized_page: &MmapPageSerialization,
        page_allocator: &Arc<PageAllocatorInner>,
    ) -> PageInner {
        let page_allocator = match self.backing_file_owner {
            BackingFileOwner::CurrentAllocator => Some(page_allocator),
            BackingFileOwner::AnotherAllocator => None,
        };
        let file_offset = serialized_page.file_offset;
        // Find the memory-mapped chunk that contains the given file offset.
        // For a file of length N bytes, there will be O(lg(N)) chunks because
        // allocation ensures that the chunk size increases exponentially.
        // New pages are likely to be in the last chunk, that's why we iterate
        // the chunks in the reverse order. The expected run-time is O(1).
        for chunk in self.chunks.iter().rev() {
            if chunk.offset <= file_offset && file_offset < chunk.offset + chunk.size as FileOffset
            {
                // If the start of the page is in the chunk, then the entire page must be in the
                // chunk.
                assert!(
                    file_offset + PAGE_SIZE as FileOffset
                        <= chunk.offset + chunk.size as FileOffset
                );
                // SAFETY: The chunk is memory-mapped, so the address range from `chunk.ptr` to
                // `chunk.ptr + chunk.size` is valid. The page is fully contained in that
                // address range.
                let page_start = unsafe { chunk.ptr.add((file_offset - chunk.offset) as usize) };
                return PageInner {
                    ptr: PagePtr(page_start),
                    offset: file_offset,
                    page_allocator: page_allocator.cloned(),
                    validation: serialized_page.validation,
                };
            }
        }
        // Unreachable based on the precondition.
        unreachable!(
            "Couldn't deserialize a page at offset {}. Current file length {}.",
            file_offset, self.file_len
        );
    }
```

**File:** rs/replicated_state/src/page_map/page_allocator.rs (L205-224)
```rust
/// Serialization-friendly representation of an mmap-based page.
#[derive(Clone, PartialEq, Debug, Deserialize, Serialize)]
pub struct MmapPageSerialization {
    pub page_index: PageIndex,
    pub file_offset: FileOffset,
    pub validation: PageValidation,
}

/// Serialization-friendly representation of `PageDelta`.
///
/// It contains sufficient information to reconstruct the page-delta
/// in another process. Note that he pages are backed by the file owned by the page allocator.
/// Each page is represented by its offset in the file. The length of the file is
/// sent along to simplify deserialization. It is guaranteed that the file
/// offsets of all pages are smaller than the length of the file.
#[derive(Clone, PartialEq, Debug, Deserialize, Serialize)]
pub struct PageDeltaSerialization {
    file_len: FileOffset,
    pages: Vec<MmapPageSerialization>,
}
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1713-1724)
```rust
        // If sandbox is compromised this value could be larger than the initial limit.
        if exec_output.wasm.num_instructions_left > message_instruction_limit {
            exec_output.wasm.num_instructions_left = message_instruction_limit;
            self.metrics
                .sandboxed_execution_instructions_left_error
                .inc();
            error!(
                self.logger,
                "[EXC-BUG] Canister {} completed execution with more instructions left than the initial limit.",
                canister_id
            )
        }
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1775-1799)
```rust
                let mut wasm_memory = execution_state.wasm_memory.clone();
                wasm_memory
                    .page_map
                    .deserialize_delta(execution_state_modifications.wasm_memory.page_delta);
                wasm_memory.size = execution_state_modifications.wasm_memory.size;
                wasm_memory.sandbox_memory = SandboxMemory::synced(wrap_remote_memory(
                    &sandbox_process,
                    next_wasm_memory_id,
                ));
                if let Err(err) = wasm_memory.verify_size() {
                    error!(
                        self.logger,
                        "{}: Canister {} has invalid wasm memory size: {}",
                        SANDBOXED_EXECUTION_INVALID_MEMORY_SIZE,
                        canister_id,
                        err
                    );
                    self.metrics
                        .sandboxed_execution_critical_error_invalid_memory_size
                        .inc();
                }
                let mut stable_memory = execution_state.stable_memory.clone();
                stable_memory
                    .page_map
                    .deserialize_delta(execution_state_modifications.stable_memory.page_delta);
```
