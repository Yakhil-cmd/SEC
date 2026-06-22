### Title
Insufficient Validation of Sandbox-Provided `PageDeltaSerialization` Data Causes Replica Panic — (`rs/replicated_state/src/page_map/page_allocator/mmap.rs`, `rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs`)

---

### Summary

The IC canister sandbox IPC path does not validate `PageDeltaSerialization` fields received from a sandbox process before using them in unsafe memory operations inside the replica. A compromised canister sandbox process can send a crafted `ExecutionFinishedRequest` containing a `file_offset` value that falls outside the mapped region, triggering an `unreachable!()` panic in the replica, or a `file_len` value so large that `mmap` fails and panics. Either path crashes the replica process. This is the direct IC analog of the Firedancer `fd_store` vulnerability: data from a sandboxed process is consumed by the controller process without sufficient field-level validation.

---

### Finding Description

The IC runs one canister sandbox process per canister. After execution, the sandbox sends an `ExecutionFinishedRequest` back to the replica via a Unix domain socket. The replica's `update_execution_state` function processes this message and calls `deserialize_delta` on the sandbox-supplied `page_delta` field without validating its contents.

**Data flow (sandbox → replica):**

```
sandbox: ExecutionFinishedRequest
  └─ exec_output.state.execution_state_modifications
       └─ wasm_memory.page_delta: PageDeltaSerialization {
              file_len: <attacker-controlled>,
              pages: [MmapPageSerialization {
                  page_index: ...,
                  file_offset: <attacker-controlled>,
                  validation: ...
              }]
          }
```

In `update_execution_state`, the delta is applied unconditionally:

```rust
wasm_memory
    .page_map
    .deserialize_delta(execution_state_modifications.wasm_memory.page_delta);
``` [1](#0-0) 

`deserialize_delta` calls `deserialize_page_delta`, which first calls `grow_for_deserialization(page_delta.file_len)` to mmap the backing file up to the sandbox-supplied length, then calls `deserialize_page` for each entry using the sandbox-supplied `file_offset`: [2](#0-1) 

**Crash Path 1 — `unreachable!()` via out-of-bounds `file_offset`:**

`deserialize_page` iterates mapped chunks looking for one that contains `file_offset`. The code comment states this is a precondition: *"File offsets of all pages are smaller than `file_len`"* — but this is never validated. If a compromised sandbox sends `file_offset >= file_len`, no chunk will contain it and the `unreachable!()` macro fires, panicking the replica: [3](#0-2) 

**Crash Path 2 — `mmap` OOM panic via oversized `file_len`:**

`grow_for_deserialization` computes `mmap_size = (file_len - self.file_len) as usize` and calls `mmap`. If `file_len` is set to a very large value (e.g., near `i64::MAX`), `mmap` fails with `ENOMEM` and the `unwrap_or_else(|err| panic!(...))` fires: [4](#0-3) 

**Crash Path 3 — `ftruncate` SIGBUS (documented by DFINITY):**

The SELinux policy document explicitly acknowledges that a sandbox process can call `ftruncate` on the shared memory file to reduce its size. If the replica has the file mmapped and accesses a truncated page, it receives `SIGBUS` and crashes. This is a known-but-unmitigated issue: [5](#0-4) 

**Acknowledged gap in `update_execution_state`:**

The code itself contains a TODO acknowledging that memory bounds from the sandbox are not validated:

```rust
// TODO: If a canister has broken out of wasm then it might have allocated more
// wasm or stable memory then allowed. We should add an additional check here
// that thet canister is still within it's allowed memory usage.
``` [6](#0-5) 

The `verify_size()` check that follows only logs an error metric — it does not abort processing or reject the malicious data: [7](#0-6) 

---

### Impact Explanation

A compromised canister sandbox process can panic the replica process. Because the replica is the deterministic state machine, a panic causes the node to restart from the last checkpoint. If the same malicious execution result is replayed (e.g., if the canister state that triggers the sandbox compromise is persisted), the node enters a crash loop. If enough nodes on a subnet are affected, subnet liveness is lost. This matches the "process-to-process crash between sandboxed tiles" impact class of the reference report.

---

### Likelihood Explanation

The prerequisite is code execution inside a canister sandbox process, which requires a Wasm sandbox escape — a separate, non-trivial vulnerability. However, the IC's threat model explicitly considers this scenario (the SELinux policy document discusses it at length), and the sandbox attack surface is large (Wasmtime, the page allocator, the IPC transport). The vulnerability class is the same as the reference report: once the sandbox boundary is crossed, the replica has no defense against malformed IPC data. Likelihood is **low-to-medium** given the prerequisite, but the consequence is severe.

---

### Recommendation

1. **Validate `file_offset` against `file_len`** in `deserialize_page_delta` before calling `deserialize_page`. Reject (return an error or kill the sandbox) if any `file_offset >= file_len`.
2. **Cap `file_len`** in `grow_for_deserialization` to a known maximum (e.g., the maximum allowed canister memory size) before calling `mmap`.
3. **Replace `unreachable!()` in `deserialize_page`** with a recoverable error path so a malformed offset kills the sandbox rather than panicking the replica.
4. **Address the `ftruncate` SIGBUS issue** noted in the SELinux policy document by sealing the memfd against truncation or by not keeping the file mmapped in the replica concurrently with sandbox access.
5. **Resolve the TODO** in `update_execution_state` by enforcing memory size bounds from the sandbox against the canister's allowed memory limit before committing state changes.

---

### Proof of Concept

A compromised sandbox (one that has escaped Wasm isolation) modifies its `execution_finished` call to send:

```rust
// In the sandbox process, after gaining code execution:
controller.execution_finished(ExecutionFinishedRequest {
    exec_id: self.exec_id,
    exec_output: SandboxExecOutput {
        state: StateModifications {
            execution_state_modifications: Some(ExecutionStateModifications {
                globals: vec![],
                wasm_memory: MemoryModifications {
                    // file_len = 4096, but file_offset = 4096 (== file_len, out of bounds)
                    page_delta: PageDeltaSerialization {
                        file_len: 4096,
                        pages: vec![MmapPageSerialization {
                            page_index: PageIndex::new(0),
                            file_offset: 4096, // >= file_len → unreachable!() in replica
                            validation: PageValidation::default(),
                        }],
                    },
                    size: NumWasmPages::new(1),
                },
                stable_memory: MemoryModifications { /* ... */ },
            }),
            system_state_modifications: Default::default(),
        },
        /* ... other fields ... */
    },
});
```

When the replica's `update_execution_state` processes this, `deserialize_page_delta` calls `grow_for_deserialization(4096)` (maps 4096 bytes), then calls `deserialize_page` with `file_offset = 4096`. No chunk covers offset 4096 (the chunk covers `[0, 4096)`), so the loop exhausts and hits:

```
thread 'CanisterSandboxIPC' panicked at 'internal error: entered unreachable code:
Couldn't deserialize a page at offset 4096. Current file length 4096.',
rs/replicated_state/src/page_map/page_allocator/mmap.rs:757
``` [3](#0-2) 

The replica process panics and restarts. The relevant IPC protocol definitions are: [8](#0-7) [9](#0-8)

### Citations

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1772-1774)
```rust
                // TODO: If a canister has broken out of wasm then it might have allocated more
                // wasm or stable memory then allowed. We should add an additional check here
                // that thet canister is still within it's allowed memory usage.
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1775-1779)
```rust
                let mut wasm_memory = execution_state.wasm_memory.clone();
                wasm_memory
                    .page_map
                    .deserialize_delta(execution_state_modifications.wasm_memory.page_delta);
                wasm_memory.size = execution_state_modifications.wasm_memory.size;
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1784-1795)
```rust
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
```

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

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L692-708)
```rust
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
```

**File:** rs/replicated_state/src/page_map/page_allocator/mmap.rs (L756-760)
```rust
        // Unreachable based on the precondition.
        unreachable!(
            "Couldn't deserialize a page at offset {}. Current file length {}.",
            file_offset, self.file_len
        );
```

**File:** ic-os/guestos/docs/SELinux-Policy.adoc (L263-265)
```text
replica is the ultimate arbiter on which files of this type are made accessible to each sandbox process. Additionally, this allows
calling ftruncate on the state files. If replica has these files mmapped concurrently, then any access to a page that has been truncated
will result in SIGBUS. This allows crashing the replica through sandbox.
```

**File:** rs/canister_sandbox/src/protocol/ctlsvc.rs (L11-17)
```rust
#[derive(Clone, PartialEq, Debug, Deserialize, Serialize)]
pub struct ExecutionFinishedRequest {
    // Id for this run, as set up by controller.
    pub exec_id: ExecId,

    pub exec_output: SandboxExecOutput,
}
```

**File:** rs/canister_sandbox/src/protocol/structs.rs (L53-58)
```rust
/// Describes the memory changes performed by execution.
#[derive(Clone, PartialEq, Debug, Deserialize, Serialize)]
pub struct MemoryModifications {
    pub page_delta: PageDeltaSerialization,
    pub size: NumWasmPages,
}
```
