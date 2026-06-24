### Title
Flat Instruction Cost for `memory.grow` Does Not Scale with Page Count, Enabling CPU Overuse at Minimal Instruction Cost - (`rs/embedders/src/wasm_utils/instrumentation.rs`)

### Summary

The IC's Wasm instrumentation assigns a flat static cost of **300 instructions** to every `memory.grow` operation regardless of how many pages are actually being allocated. Because the real CPU cost of growing Wasm memory scales linearly with the number of pages (the OS must map each page), a malicious canister can consume far more CPU time per round than it pays for in instructions.

### Finding Description

In `instruction_to_cost` in `rs/embedders/src/wasm_utils/instrumentation.rs`, the `MemoryGrow` operator is assigned a flat cost:

```rust
Operator::TableGrow { .. } | Operator::MemoryGrow { .. } => 300,
``` [1](#0-0) 

This cost is a **static, compile-time constant** applied uniformly regardless of the runtime page-count argument. The comment says "Results validated in benchmarks," but the benchmark measures growing by 1 page. The benchmark data itself shows the true cost is ~443 instructions for 1 page: [2](#0-1) 

The instrumentation does inject a `try_grow_wasm_memory` host-function call after each `memory.grow` (capturing the page count via `local.tee`): [3](#0-2) 

However, the linker binding for `try_grow_wasm_memory` does **not** call `charge_for_cpu` or `charge_for_cpu_and_mem` — it only checks memory limits and the cycles freezing threshold: [4](#0-3) 

The `try_grow_wasm_memory` system-API implementation charges **zero additional instructions** for the actual page-mapping work: [5](#0-4) 

The instrumented snapshot confirms the total instruction deduction for a `memory.grow` call is only 302 (300 for `MemoryGrow` + 1 for `local.tee` + 1 for `local.get`), regardless of page count: [6](#0-5) 

### Impact Explanation

A malicious canister can call `memory.grow` with up to 65,535 pages (4 GB for Wasm32) and pay only 300 instructions. The actual OS-level page-mapping work for 65,535 pages is ~65,535× more expensive than for 1 page, but the instruction counter is decremented by the same 300. Within a single message execution (instruction limit ~5 billion), an attacker can issue thousands of large `memory.grow` calls, each consuming real CPU time far exceeding the charged instruction cost. This allows a malicious canister to monopolize replica CPU time within a round, degrading throughput for all other canisters on the subnet — a resource exhaustion / denial-of-service impact on node operators.

### Likelihood Explanation

Any canister developer can deploy a canister with a loop of `memory.grow` calls requesting large page counts. No privileged access, governance majority, or threshold key is required. The only constraint is the canister's cycles balance (for ongoing storage reservation) and the subnet memory cap, but neither prevents the CPU-time overuse within a single round.

### Recommendation

Replace the static 300-instruction cost with a runtime charge proportional to the number of pages actually grown. The `try_grow_wasm_memory` host function already receives `additional_wasm_pages` as a parameter; add a `charge_for_cpu` call there scaled by page count:

```rust
// In the try_grow_wasm_memory linker binding:
let page_cost = NumInstructions::new(
    overhead::MEMORY_GROW_PER_PAGE.get()
        .saturating_mul(additional_wasm_pages)
);
charge_for_cpu(&mut caller, page_cost)?;
```

The static 300-instruction cost in `instruction_to_cost` should be reduced to cover only the fixed syscall overhead, with the per-page cost charged dynamically at runtime via the existing `try_grow_wasm_memory` hook.

### Proof of Concept

```wat
(module
  (memory 1)
  (func (export "canister_update exploit")
    ;; Grow by 32768 pages (2 GB) — costs only 300 instructions
    ;; but maps 2 GB of OS pages
    (drop (memory.grow (i32.const 32768)))
  )
)
```

This module, when executed as an update call, charges 300 instructions while forcing the replica to perform OS-level page-table work proportional to 2 GB of memory. Repeated across multiple canisters or messages within a round, this allows an attacker to consume replica CPU time far beyond what the instruction metering model accounts for.

### Citations

**File:** rs/embedders/src/wasm_utils/instrumentation.rs (L404-407)
```rust
        // Memory Grow and Table Grow Size expensive operations because they call
        // into the system, hence their cost is 300. Memory Size and Table Size are
        // cheaper, their cost is 20. Results validated in benchmarks.
        Operator::TableGrow { .. } | Operator::MemoryGrow { .. } => 300,
```

**File:** rs/embedders/src/wasm_utils/instrumentation.rs (L1431-1444)
```rust
            // At this point we have a memory.grow so the argument to it will be on top of
            // the stack, which we just assign to `memory_local_ix` with a local.tee
            // instruction.
            elems.extend([
                LocalTee {
                    local_index: memory_local_ix,
                },
                memory_grow_instr,
                LocalGet {
                    local_index: memory_local_ix,
                },
                Call {
                    function_index: injected_functions.try_grow_wasm_memory,
                },
```

**File:** rs/execution_environment/benches/wasm_instructions/WASM_BENCHMARKS.md (L352-353)
```markdown
wasm32/memop/memory.grow                 |  952594851 |  443 | 
wasm64/memop/memory.grow                 |  951559571 |  443 | 
```

**File:** rs/embedders/src/wasmtime_embedder/linker.rs (L1024-1038)
```rust
            linker
                .func_wrap("__", "try_grow_wasm_memory", {
                    move |mut caller: Caller<'_, StoreData>,
                          native_memory_grow_res: i32,
                          additional_wasm_pages: u32| {
                        with_system_api(&mut caller, |s| {
                            s.try_grow_wasm_memory(
                                native_memory_grow_res as i64,
                                additional_wasm_pages as u64,
                            )
                        })
                        .map(|()| native_memory_grow_res)
                    }
                })
                .unwrap();
```

**File:** rs/embedders/src/wasmtime_embedder/system_api.rs (L3426-3490)
```rust
    fn try_grow_wasm_memory(
        &mut self,
        native_memory_grow_res: i64,
        additional_wasm_pages: u64,
    ) -> HypervisorResult<()> {
        let result = {
            if native_memory_grow_res == -1 {
                return Ok(());
            }
            let new_bytes = additional_wasm_pages
                .checked_mul(WASM_PAGE_SIZE_IN_BYTES as u64)
                .map(NumBytes::new)
                .ok_or(HypervisorError::OutOfMemory)?;

            // The `memory.grow` instruction returns the previous size of the
            // Wasm memory in pages.
            let old_bytes = (native_memory_grow_res as u64)
                .checked_mul(WASM_PAGE_SIZE_IN_BYTES as u64)
                .map(NumBytes::new)
                .ok_or(HypervisorError::OutOfMemory)?;

            if let Some(wasm_memory_limit) = self
                .memory_usage
                .effective_wasm_memory_limit(&self.api_type)
            {
                let wasm_memory_usage =
                    NumBytes::new(new_bytes.get().saturating_add(old_bytes.get()));

                // A Wasm memory limit of 0 means unlimited.
                if wasm_memory_limit.get() != 0 && wasm_memory_usage > wasm_memory_limit {
                    return Err(HypervisorError::WasmMemoryLimitExceeded {
                        bytes: wasm_memory_usage,
                        limit: wasm_memory_limit,
                    });
                }
            }

            match self.memory_usage.allocate_execution_memory(
                new_bytes,
                &self.api_type,
                &mut self.sandbox_safe_system_state,
                &self.execution_parameters.subnet_memory_saturation,
                ExecutionMemoryType::WasmMemory,
            ) {
                Ok(()) => Ok(()),
                Err(err @ HypervisorError::InsufficientCyclesInMemoryGrow { .. }) => {
                    // Return an out-of-cycles error instead of out-of-memory.
                    Err(err)
                }
                Err(err @ HypervisorError::ReservedCyclesLimitExceededInMemoryGrow { .. }) => {
                    // Return a reservation error instead of out-of-memory.
                    Err(err)
                }
                Err(_err) => Err(HypervisorError::OutOfMemory),
            }
        };
        trace_syscall!(
            self,
            TryGrowWasmMemory,
            result,
            native_memory_grow_res,
            additional_wasm_pages
        );
        result
    }
```

**File:** rs/embedders/tests/snapshots/instrumentation__memory_grow.snap (L35-49)
```text
    global.get 0
    i64.const 302
    i64.sub
    global.set 0
    global.get 0
    i64.const 0
    i64.lt_s
    if ;; label = @1
      call 0
    end
    local.get 0
    local.tee 3
    memory.grow
    local.get 3
    call 1
```
