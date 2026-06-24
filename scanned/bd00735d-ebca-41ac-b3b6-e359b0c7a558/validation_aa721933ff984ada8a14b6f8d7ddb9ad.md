### Title
Missing Globals Length/Type Validation in `update_execution_state` Allows Compromised Sandbox to Brick Canister via Corrupted Replicated State — (`rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs`)

---

### Summary

A compromised sandbox process can return an `ExecutionStateModifications.globals` vector with a different length or incompatible types than the original `exported_globals` sent in `SandboxExecInput`. The replica's `update_execution_state` function accepts this vector without any length or type validation and persists it to replicated state. On the next execution, the corrupted globals are fed back into a new Wasm instance, triggering a `fatal!()` panic in the sandbox process due to the length mismatch, effectively bricking the canister permanently.

---

### Finding Description

**Step 1 — No validation in `update_execution_state`:**

The function at `rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs:1752` receives `execution_state_modifications` from the sandbox over IPC and blindly wraps `execution_state_modifications.globals` into `ExecutionStateChanges` without comparing its length or element types against `execution_state.exported_globals`: [1](#0-0) 

Notably, the same function already guards against another sandbox-compromise vector — `num_instructions_left` — with an explicit comment "If sandbox is compromised this value could be larger than the initial limit": [2](#0-1) 

The globals vector has no equivalent guard.

**Step 2 — Corrupted globals committed to replicated state:**

`apply_canister_state_changes` in `rs/execution_environment/src/execution/common.rs` applies the globals unconditionally: [3](#0-2) 

The `ExecutionStateModifications` struct itself has no invariant enforcement: [4](#0-3) 

**Step 3 — Fatal panic on next execution:**

On the next execution, the replica sends the now-corrupted `exported_globals` (N+K entries) back to the sandbox as `SandboxExecInput.globals`. Inside the sandbox, `wasmtime_embedder.rs` checks the count against the actual Wasm instance's exported globals (N entries) and calls `fatal!()`: [5](#0-4) 

A type mismatch (e.g., `Global::I64` where the Wasm module declares `i32`) triggers a second `fatal!()` path: [6](#0-5) 

Both paths are confirmed to panic by the test suite: [7](#0-6) 

---

### Impact Explanation

The corrupted globals vector is persisted to replicated state. Every subsequent execution attempt for that canister sends the corrupted globals to the sandbox, which panics on the length/type check. The canister is permanently bricked — no update, query, or heartbeat can execute. This matches the stated scope of **canister integrity loss via corrupted Wasm global state persisted to replicated state**.

---

### Likelihood Explanation

**Low.** Exploiting this requires a prior Wasm engine sandbox escape — the canister's Wasm code alone cannot modify the IPC protocol between the sandbox process and the replica. The attacker must first achieve arbitrary code execution within the sandbox process (e.g., via a Wasmtime memory-safety bug), then craft a malformed `ExecutionFinishedRequest` with extra globals. This is a two-step exploit. The missing validation is a defense-in-depth failure that amplifies the impact of a sandbox escape, rather than a standalone vulnerability reachable from ingress alone.

---

### Recommendation

In `update_execution_state`, after receiving `execution_state_modifications` from the sandbox, add a guard analogous to the existing `num_instructions_left` check:

```rust
// Guard: sandbox cannot change the number or types of exported globals
if execution_state_modifications.globals.len() != execution_state.exported_globals.len() {
    error!(self.logger, "[EXC-BUG] Canister {} sandbox returned {} globals, expected {}",
        canister_id,
        execution_state_modifications.globals.len(),
        execution_state.exported_globals.len());
    self.metrics.sandboxed_execution_critical_error_invalid_globals.inc();
    // truncate or reject rather than persist
}
```

Type validation (matching `Global::I32` to `Global::I32`, etc.) should also be enforced element-by-element before committing.

---

### Proof of Concept

Differential test (pseudocode):

```rust
// 1. Deploy canister with N exported globals
// 2. Intercept sandbox IPC, inject ExecutionFinishedRequest with N+K globals
// 3. Assert replica persists N+K globals (no rejection)
// 4. Trigger next execution
// 5. Assert sandbox panics with "Given number of exported globals N+K is not equal
//    to the number of instance exported globals N"
// 6. Assert canister is permanently unexecutable
```

The `#[should_panic]` test at `rs/embedders/tests/wasmtime_embedder.rs:465` already confirms step 5 in isolation. [8](#0-7)

### Citations

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

**File:** rs/canister_sandbox/src/replica_controller/sandboxed_execution_controller.rs (L1817-1824)
```rust
                CanisterStateChanges {
                    execution_state_changes: Some(ExecutionStateChanges {
                        globals: execution_state_modifications.globals,
                        wasm_memory,
                        stable_memory,
                    }),
                    system_state_modifications,
                }
```

**File:** rs/execution_environment/src/execution/common.rs (L582-591)
```rust
            if let Some(ExecutionStateChanges {
                globals,
                wasm_memory,
                stable_memory,
            }) = execution_state_changes
            {
                execution_state.wasm_memory = wasm_memory;
                execution_state.stable_memory = stable_memory;
                execution_state.exported_globals = globals;
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

**File:** rs/embedders/src/wasmtime_embedder.rs (L512-519)
```rust
            if exported_globals.len() != instance_globals.len() {
                fatal!(
                    self.log,
                    "Given number of exported globals {} is not equal to the number of instance exported globals {}",
                    exported_globals.len(),
                    instance_globals.len()
                );
            }
```

**File:** rs/embedders/src/wasmtime_embedder.rs (L539-554)
```rust
                        .unwrap_or_else(|e| {
                            let v = match v {
                                Global::I32(val) => (val).to_string(),
                                Global::I64(val) => (val).to_string(),
                                Global::F32(val) => (val).to_string(),
                                Global::F64(val) => (val).to_string(),
                                Global::V128(val) => (val).to_string(),
                            };
                            fatal!(
                                self.log,
                                "error while setting exported global {} to {}: {}",
                                ix,
                                v,
                                e
                            )
                        })
```

**File:** rs/embedders/tests/wasmtime_embedder.rs (L443-497)
```rust
#[test]
#[should_panic(expected = "attempt to set global to value of wrong type")]
fn try_to_set_globals_with_wrong_types() {
    let _instance = WasmtimeInstanceBuilder::new()
        .with_wat(
            r#"
                    (module
                      (global (export "g1") (mut i64) (i64.const 0))
                      (global (export "g2") (mut i32) (i32.const 42))
                    )"#,
        )
        // Should fail because of not correct type of the second one.
        .with_globals(vec![
            Global::I64(5),
            Global::I64(12),
            // Last global is the instruction counter which will be
            // overwritten anyway.
            Global::I64(0),
        ])
        .build();
}

#[test]
#[should_panic(
    expected = "Given number of exported globals 3 is not equal to the number of instance exported globals 2"
)]
fn try_to_set_globals_that_are_more_than_the_instance_globals() {
    let _instance = WasmtimeInstanceBuilder::new()
        // Module only exports one global, but instrumentation adds a second.
        .with_wat(
            r#"
                (module
                    (global (export "g") (mut i64) (i64.const 42))
                )"#,
        )
        .with_globals(vec![Global::I64(0); 3])
        .build();
}

#[test]
#[should_panic(
    expected = "Given number of exported globals 1 is not equal to the number of instance exported globals 2"
)]
fn try_to_set_globals_that_are_less_than_the_instance_globals() {
    let _instance = WasmtimeInstanceBuilder::new()
        // Module only exports one global, but instrumentation adds a second.
        .with_wat(
            r#"
                (module
                    (global (export "g") (mut i64) (i64.const 42))
                )"#,
        )
        .with_globals(vec![Global::I64(0); 1])
        .build();
}
```
