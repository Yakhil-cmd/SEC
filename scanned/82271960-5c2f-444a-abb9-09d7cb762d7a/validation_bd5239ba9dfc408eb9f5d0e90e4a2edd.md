### Title
Gzip Footer Manipulation Allows Round Instruction Budget Exhaustion via `create_execution_state` - (`rs/execution_environment/src/hypervisor.rs`, `rs/embedders/src/wasm_utils/decoding.rs`)

---

### Summary

An unprivileged canister developer can submit a gzip-compressed Wasm module whose RFC-1952 footer encodes `u32::MAX` (0xFFFFFFFF) as the uncompressed size while keeping the actual payload tiny. `decoded_wasm_size` returns 4,294,967,295 without any cap, `create_execution_state` computes a `compilation_cost` of ~25.7 trillion instructions from that value, and then deducts that entire amount from `round_limits.instructions` in the error path — before any other canister message executes in the same round.

---

### Finding Description

**Step 1 — Attacker-controlled size with no cap**

`wasm_encoding_and_size` reads the last 4 bytes of the gzip stream as a raw `u32` and casts directly to `usize`:

```rust
let uncompressed_size = u32::from_le_bytes(isize_bytes) as usize;
return Ok((WasmEncoding::Gzip, uncompressed_size));
```

The function's own doc-comment acknowledges this is untrusted:

> "If the Wasm is gzipped, then the returned size cannot be trusted. It would come from the gzip footer which could have been manipulated." [1](#0-0) 

**Step 2 — Uncapped size drives `compilation_cost`**

`Hypervisor::create_execution_state` calls `decoded_wasm_size`, takes the result at face value, and immediately multiplies by `cost_to_compile_wasm_instruction` (default: **6,000**):

```rust
let wasm_size_result = decoded_wasm_size(canister_module.as_slice());
let wasm_size = match wasm_size_result {
    Ok(size) => std::cmp::max(size, canister_module.len()),
    Err(_) => canister_module.len(),
};
let compilation_cost = self.cost_to_compile_wasm_instruction * wasm_size as u64;
```

With `wasm_size = 4,294,967,295`: `compilation_cost = 6,000 × 4,294,967,295 = 25,769,803,770,000` instructions. No overflow (fits in `u64`), no cap. [2](#0-1) [3](#0-2) 

**Step 3 — `decode_wasm` rejects the module, but the damage is already done**

`wasm_executor.create_execution_state` calls `decode_wasm` with `max_size = 100 MiB`. Since 4,294,967,295 > 100 MiB, `decode_wasm` returns `Err(ModuleTooLarge)`:

```rust
if uncompressed_size as u64 > max_size.get() {
    return Err(WasmValidationError::ModuleTooLarge { ... });
}
``` [4](#0-3) [5](#0-4) 

**Step 4 — The `Err` branch deducts the manipulated cost from `round_limits`**

Back in `Hypervisor::create_execution_state`, the `Err` arm uses the pre-computed, attacker-inflated `compilation_cost`:

```rust
Err(err) => {
    let total_cost = self.create_execution_state_base_cost + compilation_cost;
    round_limits.instructions -= as_round_instructions(total_cost);
    (total_cost, Err(err))
}
```

`round_limits.instructions` is a signed `i64`-backed counter. Subtracting ~25.7 trillion from a typical round budget of ~7 billion drives it deeply negative. The scheduler interprets a non-positive `round_limits.instructions` as "round exhausted" and stops executing any further messages. [6](#0-5) 

**No upstream guard exists.** `execute_install` calls `create_execution_state` directly after assembling the module, with no pre-check on the decoded size: [7](#0-6) 

---

### Impact Explanation

- Every execution round in which the attacker submits this crafted `install_code` message has its instruction budget exhausted before any other canister message runs.
- The attack is repeatable every round at the cost of one `install_code` call (which fails, but the round budget is already gone).
- All other canisters on the same subnet are starved of execution for that round, constituting a subnet-wide DoS.

---

### Likelihood Explanation

- Requires only: a canister ID (create one with minimal cycles), a ~20-byte crafted gzip blob, and an `install_code` ingress call.
- No privileged access, no key material, no governance majority needed.
- Locally testable and reproducible.

---

### Recommendation

Cap `wasm_size` to `wasm_max_size` before computing `compilation_cost` in `Hypervisor::create_execution_state`. Since `decoded_wasm_size` is explicitly documented as untrusted for gzip, the cost estimate must be bounded:

```rust
let wasm_size = wasm_size.min(self.wasm_max_size.get() as usize);
let compilation_cost = self.cost_to_compile_wasm_instruction * wasm_size as u64;
```

Alternatively, move the `decode_wasm` size check to occur **before** `compilation_cost` is computed, so an oversized footer causes an early `Err` return that charges only the base cost.

---

### Proof of Concept

```
# Minimal valid gzip: 10-byte header + 2-byte empty deflate + 8-byte footer
# Footer: CRC32=0x00000000, ISIZE=0xFFFFFFFF
bytes = b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03'  # gzip header
bytes += b'\x03\x00'                                    # empty deflate block
bytes += b'\x00\x00\x00\x00'                           # CRC32
bytes += b'\xff\xff\xff\xff'                           # ISIZE = u32::MAX

# Submit via install_code to any canister you control.
# Observe: round_limits.instructions drops by ~25.7 trillion.
# All other canister messages in the same round are skipped.
```

### Citations

**File:** rs/embedders/src/wasm_utils/decoding.rs (L11-41)
```rust
/// # Warning
///
/// If the Wasm is gzipped, then the returned size cannot be trusted. It would
/// come from the gzip footer which could have been manipulated.
fn wasm_encoding_and_size(
    module_bytes: &[u8],
) -> Result<(WasmEncoding, usize), WasmValidationError> {
    // \0asm is WebAssembly module magic bytes.
    // https://webassembly.github.io/spec/core/binary/modules.html#binary-module
    if module_bytes.starts_with(b"\x00asm") {
        return Ok((WasmEncoding::Wasm, module_bytes.len()));
    }

    // 1f 8b is GZIP magic number, 08 is DEFLATE algorithm.
    // https://datatracker.ietf.org/doc/html/rfc1952.html#page-6
    if module_bytes.starts_with(b"\x1f\x8b\x08") {
        // There should be at least an 8-byte header and an 8-byte footer.
        if module_bytes.len() < 16 {
            return Err(WasmValidationError::DecodingError(
                "invalid Wasm module: gzip stream is too short".to_string(),
            ));
        }

        // Get the uncompressed size from the footer.
        // The size is in the last 4 bytes in little-endian encoding.
        // https://datatracker.ietf.org/doc/html/rfc1952.html#page-5
        let mut isize_bytes = [0_u8; 4];
        // We checked the size in advance so it's safe to access the last 4 bytes.
        isize_bytes.copy_from_slice(&module_bytes[module_bytes.len() - 4..module_bytes.len()]);
        let uncompressed_size = u32::from_le_bytes(isize_bytes) as usize;
        return Ok((WasmEncoding::Gzip, uncompressed_size));
```

**File:** rs/embedders/src/wasm_utils/decoding.rs (L69-74)
```rust
    if uncompressed_size as u64 > max_size.get() {
        return Err(WasmValidationError::ModuleTooLarge {
            size: uncompressed_size as u64,
            allowed: max_size.get(),
        });
    }
```

**File:** rs/execution_environment/src/hypervisor.rs (L155-160)
```rust
        let wasm_size_result = decoded_wasm_size(canister_module.as_slice());
        let wasm_size = match wasm_size_result {
            Ok(size) => std::cmp::max(size, canister_module.len()),
            Err(_) => canister_module.len(),
        };
        let compilation_cost = self.cost_to_compile_wasm_instruction * wasm_size as u64;
```

**File:** rs/execution_environment/src/hypervisor.rs (L188-192)
```rust
            Err(err) => {
                let total_cost = self.create_execution_state_base_cost + compilation_cost;
                round_limits.instructions -= as_round_instructions(total_cost);
                (total_cost, Err(err))
            }
```

**File:** rs/config/src/embedders.rs (L57-58)
```rust
pub(crate) const DEFAULT_COST_TO_COMPILE_WASM_INSTRUCTION: NumInstructions =
    NumInstructions::new(6_000);
```

**File:** rs/embedders/src/wasm_executor.rs (L421-424)
```rust
                    let decoded_wasm: Cow<'_, BinaryEncodedWasm> = Cow::Owned(decode_wasm(
                        self.wasm_embedder.config().wasm_max_size,
                        wasm_binary.binary.to_shared_vec(),
                    )?);
```

**File:** rs/execution_environment/src/execution/install.rs (L117-122)
```rust
    let (instructions_from_compilation, result) = round.hypervisor.create_execution_state(
        wasm_module,
        canister_id,
        round_limits,
        original.compilation_cost_handling,
    );
```
