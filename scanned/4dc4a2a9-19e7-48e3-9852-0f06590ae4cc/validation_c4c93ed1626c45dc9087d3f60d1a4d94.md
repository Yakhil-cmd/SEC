### Title
Missing Explicit `wasm_threads(false)` in Wasmtime Config Enables Deterministic Execution Divergence on Wasmtime Minor-Version Upgrade - (File: rs/embedders/src/wasm_utils/validation.rs)

---

### Summary

`wasmtime_validation_config` — the single function that produces the `wasmtime::Config` used for both Wasm validation and execution across every replica — never calls `config.wasm_threads(false)`. The project's own documentation explicitly lists this call as mandatory for determinism. The `wasmtime` dependency is pinned only to a floating caret constraint (`^45.0.0`), so any 45.x minor-version bump that changes the default for the threads proposal would silently allow non-deterministic canister execution, breaking subnet consensus.

---

### Finding Description

`wasmtime_validation_config` in `rs/embedders/src/wasm_utils/validation.rs` starts from `wasmtime::Config::default()` and then explicitly sets every determinism-critical flag — except `wasm_threads`. [1](#0-0) 

The function's own comment says the list is kept in alphabetical order "to simplify comparison with new `wasmtime::Config` methods in a new version of wasmtime," implying it is meant to be exhaustive. Yet `wasm_threads` — which falls alphabetically between `wasm_tail_call` and `wasm_extended_const` — is absent. A `grep` across the entire `rs/` tree for `wasm_threads` returns **zero matches**, confirming the call is missing everywhere in production code. [2](#0-1) 

The README explicitly documents: *"We use the following config flags to ensure deterministic execution in Wasmtime: Threads: `wasmtime::Config::wasm_threads(false)`."* The production config relies entirely on wasmtime's current default (threads disabled) rather than enforcing it.

`wasmtime_execution_config` — used to actually run canisters — is derived directly from `wasmtime_validation_config` and inherits the same omission: [3](#0-2) 

The `wasmtime` Bazel dependency uses a floating caret constraint: [4](#0-3) 

`^45.0.0` resolves to `>=45.0.0, <46.0.0`. Any 45.x release that promotes the threads proposal to enabled-by-default would change the effective config without any code change in the IC repository.

---

### Impact Explanation

The Wasm threads proposal introduces shared linear memory and atomic operations. If `wasm_threads` is enabled, a canister Wasm module can declare a shared memory and use atomic instructions. Atomic operations on shared memory are a documented source of non-determinism in WebAssembly (see the IC's own README reference to the Wasm non-determinism spec). Different replicas executing the same canister message could observe different memory states and produce different output values and dirty-page sets, causing diverging state hashes. The state manager would detect the divergence and the affected replica would be forced to roll back and re-sync, stalling the subnet.

---

### Likelihood Explanation

Wasmtime actively develops the threads proposal; it is already feature-complete in the engine and gated only by a config flag. A minor-version bump (45.x) that flips the default is realistic. The IC's own lock-file update workflow (updating `Cargo.lock` / Bazel lock) is the only trigger needed — no attacker action is required beyond submitting a Wasm module that uses shared memory, which any canister developer can do via the standard `install_code` management canister call.

---

### Recommendation

Add the following explicit call inside `wasmtime_validation_config`, in alphabetical order between `wasm_tail_call` and the closing block:

```rust
// Threads are disabled for determinism (see README).
config.wasm_threads(false);
```

This mirrors the pattern already used for `wasm_relaxed_simd`: [5](#0-4) 

Additionally, the test `test_initial_wasmtime_config` should be updated to assert the explicit flag rather than relying on the default: [6](#0-5) 

---

### Proof of Concept

1. A canister developer compiles a Wasm module with a shared memory declaration:
   ```wat
   (module
     (memory (export "memory") 1 1 shared)
     (func (export "canister_update test")
       (i32.atomic.store (i32.const 0) (i32.const 42))
     )
   )
   ```
2. Under the current wasmtime 45.x default, `wasmtime::Module::validate` rejects this because threads are off by default — the module never installs.
3. After a wasmtime 45.x minor bump that enables threads by default, `wasmtime_validation_config` produces a config with threads enabled (since `wasm_threads(false)` is never called). The module passes validation and is installed.
4. At execution time, atomic operations on shared memory produce implementation-defined ordering results that differ across replicas (different CPU microarchitectures, NUMA topologies, OS schedulers). Replicas compute different exported globals / dirty pages, diverging state hashes are reported to the state manager, and the subnet stalls.

### Citations

**File:** rs/embedders/src/wasm_utils/validation.rs (L1699-1729)
```rust
pub fn wasmtime_validation_config(_embedders_config: &EmbeddersConfig) -> wasmtime::Config {
    let mut config = wasmtime::Config::default();

    // Keep this in the alphabetical order to simplify comparison with new
    // `wasmtime::Config` methods in a new version of wasmtime.

    // NaN canonicalization is needed for determinism.
    config.cranelift_nan_canonicalization(true);
    // Disable optimizations to keep compilation simple and fast.
    // The assumption is that Wasm binaries have already been optimized.
    config.cranelift_opt_level(wasmtime::OptLevel::None);
    // Disabling the address map saves about 20% of compile code size.
    config.generate_address_map(false);
    // The signal handler uses Posix signals, not Mach ports on MacOS.
    config.macos_use_mach_ports(false);
    config.wasm_backtrace_max_frames(NonZero::new(20_usize));
    config.wasm_backtrace_details(wasmtime::WasmBacktraceDetails::Disable);
    config.wasm_bulk_memory(true);
    config.wasm_function_references(false);
    config.wasm_gc(false);
    config.wasm_memory64(true);
    // Wasm multi-memory feature is disabled during validation,
    // but enabled during execution for the Wasm-native stable memory
    // implementation.
    config.wasm_multi_memory(false);
    config.wasm_reference_types(true);
    // The relaxed SIMD instructions are disable for determinism.
    config.wasm_relaxed_simd(false);
    config.wasm_tail_call(true);
    // WebAssembly extended-const proposal is disabled.
    config.wasm_extended_const(false);
```

**File:** rs/embedders/README.adoc (L1-11)
```text
# WebAssembly engine embedders 

This crate defines helpers for embedding WebAssembly engines. Currently only the [Wasmtime](https://github.com/bytecodealliance/wasmtime) is supported.

## Nondeterminism

See [Nondeterminism in WebAssembly](https://github.com/WebAssembly/design/blob/main/Nondeterminism.md) for general description of sources of nondeterminism.
We use the following config flags to ensure deterministic execution in Wasmtime:

- Threads: `wasmtime::Config::wasm_threads(false)`.
- NaN values: `wasmtime::Config::cranelift_nan_canonicalization(true)`.
```

**File:** rs/embedders/src/wasmtime_embedder.rs (L270-276)
```rust
    pub fn wasmtime_execution_config(embedder_config: &EmbeddersConfig) -> wasmtime::Config {
        let mut config = wasmtime_validation_config(embedder_config);

        config.wasm_multi_memory(true);
        config.wasm_memory64(true);
        config
    }
```

**File:** bazel/rust.MODULE.bazel (L1871-1882)
```text
crate.spec(
    default_features = False,
    features = [
        "cranelift",
        "gc",
        "gc-null",
        "parallel-compilation",
        "runtime",
    ],
    package = "wasmtime",
    version = "^45.0.0",
)
```

**File:** rs/embedders/src/wasmtime_embedder/tests.rs (L177-244)
```rust
#[test]
fn test_initial_wasmtime_config() {
    // The following proposals should be disabled: simd, relaxed_simd,
    // threads, multi_memory, exceptions, extended_const, component_model,
    // function_references, memory_control, gc
    for (proposal, _url, wat, expected_err_msg) in [
        (
            "relaxed_simd",
            "https://github.com/WebAssembly/relaxed-simd/",
            "(module (func $f (param v128) (drop (f64x2.relaxed_madd (local.get 0) (local.get 0) (local.get 0)))))",
            "relaxed SIMD support is not enabled",
        ),
        (
            "threads",
            "https://github.com/WebAssembly/threads/",
            r#"(module (import "env" "memory" (memory 1 1 shared)))"#,
            "threads must be enabled",
        ),
        (
            "multi_memory",
            "https://github.com/WebAssembly/multi-memory/",
            "(module (memory $m1 1 1) (memory $m2 1 1))",
            "failed with multiple memories",
        ),
        // Exceptions
        (
            "extended_const",
            "https://github.com/WebAssembly/extended-const/",
            "(module (global i32 (i32.add (i32.const 0) (i32.const 0))))",
            "constant expression required",
        ),
        (
            "component_model",
            "https://github.com/WebAssembly/component-model/",
            "(component (core module (func $f)))",
            "component model feature is not enabled",
        ),
        (
            "function_references",
            "https://github.com/WebAssembly/function-references/",
            "(module (type $t (func (param i32))) (func $fn (param $f (ref $t))))",
            "function references required for index reference types",
        ),
        // Memory control
        // GC
    ] {
        let wasm_binary = BinaryEncodedWasm::new(wat::parse_str(wat).unwrap_or_else(|err| {
            panic!("Error parsing proposal `{proposal}` code snippet: {err}")
        }));
        let err = validate_and_instrument_for_testing(
            &WasmtimeEmbedder::new(EmbeddersConfig::default(), no_op_logger()),
            &wasm_binary,
        )
        .err()
        .unwrap_or_else(|| {
            panic!("Error having `{proposal}` proposal enabled in the `wasmtime` config.")
        });
        // Format error message with cause using '{:?}'
        let err_msg = format!("{err:?}");
        // Verify that the error occurred because the expected feature was disabled.
        // If this test fails, check whether:
        // 1. The feature being tested is enabled by default (in that case, explicitly disable it in the config), or
        // 2. The error message has changed in a new release (update the expected error message accordingly).
        assert!(
            err_msg.contains(expected_err_msg),
            "Error expecting `{expected_err_msg}`, but got `{err_msg}`"
        );
    }
```
