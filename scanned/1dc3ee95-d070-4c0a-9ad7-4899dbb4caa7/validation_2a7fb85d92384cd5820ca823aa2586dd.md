### Title
Unvalidated Sandbox-Provided Memory Size Bypasses Per-Canister Memory Limits in `update_execution_state` - (File: `rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs`)

---

### Summary

The IC replica controller unconditionally applies `wasm_memory.size` and `stable_memory.size` values received from the canister sandbox process over IPC without checking them against the canister's allowed memory allocation. A compromised sandbox process (reachable via a malicious canister exploiting the Wasm execution engine) can report an arbitrarily inflated memory size, bypassing per-canister memory limit enforcement in the replica. The codebase itself contains an explicit TODO acknowledging this gap.

---

### Finding Description

The IC canister sandbox architecture runs each canister's Wasm execution in a separate process. When execution finishes, the sandbox sends an `ExecutionFinishedRequest` containing a `SandboxExecOutput` back to the replica controller over a Unix domain socket IPC channel. [1](#0-0) 

The `SandboxExecOutput` includes `StateModifications`, which contains `ExecutionStateModifications` with `MemoryModifications` for both Wasm and stable memory. Each `MemoryModifications` carries a `page_delta` (dirty pages) and a `size` (the new memory size as reported by the sandbox). [2](#0-1) 

In `update_execution_state`, the replica controller applies these sandbox-provided values directly to the canonical execution state:

```rust
wasm_memory.size = execution_state_modifications.wasm_memory.size;
// ...
stable_memory.size = execution_state_modifications.stable_memory.size;
``` [3](#0-2) 

The only check performed is `verify_size()`, which validates against the **absolute maximum** (e.g., 4 GiB for Wasm32), not against the **canister's allocated memory limit**. The codebase explicitly acknowledges this gap with a TODO comment:

```rust
// TODO: If a canister has broken out of wasm then it might have allocated more
// wasm or stable memory then allowed. We should add an additional check here
// that thet canister is still within it's allowed memory usage.
``` [4](#0-3) 

This is structurally identical to the Firedancer vulnerability: data received from a peer process over IPC (`fd_pack` → `fd_bank` in Firedancer; sandbox → replica controller in IC) is used without sufficient validation. The IC team already recognized the analogous `num_instructions_left` field needed clamping for the same reason ("If sandbox is compromised this value could be larger than the initial limit"): [5](#0-4) 

The same defensive treatment was never applied to the memory size fields.

---

### Impact Explanation

A compromised sandbox process can send a `SandboxExecOutput` with an inflated `wasm_memory.size` or `stable_memory.size`. The replica controller applies this value to the canonical `ExecutionState` without checking it against the canister's `MemoryAllocation` or the subnet's available memory. Consequences include:

1. **Per-canister memory limit bypass**: The canister's reported memory usage in the replicated state exceeds its actual allocation, corrupting the subnet's memory accounting.
2. **Subnet memory exhaustion**: By inflating reported memory, a malicious canister can cause the subnet to believe it has consumed all available memory, preventing other canisters from growing their memory (effective DoS against the subnet).
3. **State certification divergence**: If nodes disagree on the reported memory size (e.g., due to a race or partial compromise), state hash divergence could occur.

The impact class is **canister isolation break** and **cycles/resource accounting bug**.

---

### Likelihood Explanation

The attack requires two steps:

1. **Deploy a malicious canister** — any unprivileged canister developer can do this.
2. **Escape the Wasm sandbox** — exploit a vulnerability in `wasmtime` or the sandbox process to gain arbitrary code execution within the sandbox process.

Step 2 is a significant prerequisite, but it is the exact threat model the sandbox architecture is designed to contain. The IC team explicitly acknowledges this threat model in the `num_instructions_left` clamping code and in the SELinux policy documentation: [6](#0-5) 

Once sandbox code execution is achieved, the IPC channel to the replica controller is the natural escalation path — exactly as in the Firedancer report. The missing memory size validation is the specific gap that makes this escalation impactful beyond a simple crash.

---

### Recommendation

In `update_execution_state`, after applying `wasm_memory.size` and `stable_memory.size` from the sandbox, add an explicit check that the reported sizes do not exceed the canister's `MemoryAllocation` and the subnet's available memory. If the check fails, treat it as a sandbox integrity violation (log a critical error, reject the state changes, and terminate the sandbox process). This mirrors the existing defensive clamping already applied to `num_instructions_left`. [7](#0-6) 

---

### Proof of Concept

A compromised sandbox process (e.g., a canister that has exploited a wasmtime bug) sends the following crafted `ExecutionFinishedRequest` over the IPC socket:

```rust
ExecutionFinishedRequest {
    exec_id: <valid_exec_id>,
    exec_output: SandboxExecOutput {
        wasm: WasmExecutionOutput {
            wasm_result: Ok(Some(WasmResult::Reply(vec![]))),
            num_instructions_left: <valid_value>,
            // ... other fields normal
        },
        state: StateModifications {
            execution_state_modifications: Some(ExecutionStateModifications {
                globals: vec![],
                wasm_memory: MemoryModifications {
                    page_delta: <empty_delta>,
                    // Inflate to near-maximum, far beyond canister's allocation
                    size: NumWasmPages::new(65536), // 4 GiB
                },
                stable_memory: MemoryModifications {
                    page_delta: <empty_delta>,
                    size: NumWasmPages::new(65536), // 4 GiB
                },
            }),
            system_state_modifications: SystemStateModifications::default(),
        },
        // ...
    },
}
```

The replica controller's `update_execution_state` applies `wasm_memory.size = 65536` pages to the canonical state. `verify_size()` passes (it is within the absolute maximum). The subnet's memory accounting now believes this canister occupies 4 GiB, blocking all other canisters from allocating memory. [8](#0-7) [9](#0-8)

### Citations

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

**File:** rs/canister_sandbox/src/protocol/structs.rs (L76-86)
```rust
#[derive(Clone, PartialEq, Debug, Deserialize, Serialize)]
pub struct ExecutionStateModifications {
    /// The state of the global variables after execution.
    pub globals: Vec<Global>,

    /// Modifications in the Wasm memory.
    pub wasm_memory: MemoryModifications,

    /// Modifications in the stable memory.
    pub stable_memory: MemoryModifications,
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

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1772-1774)
```rust
                // TODO: If a canister has broken out of wasm then it might have allocated more
                // wasm or stable memory then allowed. We should add an additional check here
                // that thet canister is still within it's allowed memory usage.
```

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1775-1800)
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
                stable_memory.size = execution_state_modifications.stable_memory.size;
```

**File:** ic-os/guestos/docs/SELinux-Policy.adoc (L261-265)
```text
_Side effects_: Formally it allows sandbox to read/write arbitrary state files set up by replica (even those of other canisters). However,
sandbox cannot actively _open_ any of these files. It can in fact only access files through descriptors that are passed by replica. So
replica is the ultimate arbiter on which files of this type are made accessible to each sandbox process. Additionally, this allows
calling ftruncate on the state files. If replica has these files mmapped concurrently, then any access to a page that has been truncated
will result in SIGBUS. This allows crashing the replica through sandbox.
```
