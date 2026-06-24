### Title
Gzip Decompression of Canister WASM Performed Before Cycles Are Prepaid - (File: rs/execution_environment/src/canister_manager.rs)

### Summary
In `install_code_dts`, the function `wasm_execution_mode` is called to determine whether the submitted WASM is 32-bit or 64-bit (for pricing purposes). This function performs a full gzip decompression of the submitted module **before** any cycles are prepaid. An attacker can submit a crafted gzip-compressed WASM that triggers expensive decompression work in the replica process without paying any cycles for it, because the decompression failure causes a silent fallback to `Wasm32` mode and the prepay step is never reached.

### Finding Description

In `install_code_dts` in `rs/execution_environment/src/canister_manager.rs`, the call sequence is:

1. **Line 861**: `wasm_execution_mode(context.wasm_source.clone())` — calls `decode_wasm()` internally, performing full gzip decompression.
2. **Line 870**: `prepay_execution_cycles(...)` — cycles are prepaid only after decompression has already occurred. [1](#0-0) 

The `wasm_execution_mode` helper function at the bottom of the same file calls `into_canister_module()` and then `decode_wasm()`, which performs the full gzip decompression stream read: [2](#0-1) 

The `decode_wasm` function in `rs/embedders/src/wasm_utils/decoding.rs` reads the uncompressed size from the gzip footer (attacker-controlled, up to `wasm_max_size` bytes), then streams up to `uncompressed_size + 1` bytes through the decompressor: [3](#0-2) 

If decompression fails (e.g., corrupted payload), `wasm_execution_mode` silently returns `Wasm32` and execution continues to the prepay step. No cycles are charged for the decompression work that already occurred. If decompression succeeds, the same decompression is performed a second time inside the sandbox during `create_execution_state`, meaning a successful install causes double decompression with only the second charged.

For `WasmSource::CanisterModule` (the direct `install_code` path), `instructions_to_assemble()` returns zero, so no assembly charge is applied before the decompression either: [4](#0-3) 

### Impact Explanation

The decompression work is bounded by `min(footer_uncompressed_size, wasm_max_size)`. With `wasm_max_size` typically set to 100 MB, an attacker can force up to ~100 MB of decompression work per `install_code` call in the replica process, with zero cycles charged if the decompression fails. This is CPU work performed in the replica process (not the sandbox), directly consuming replica resources. Repeated calls can degrade subnet throughput for all canisters on the subnet.

**Vulnerability class**: Cycles/resource accounting bug — computationally expensive operation triggered before resource charge.

### Likelihood Explanation

Any canister controller (an unprivileged role) can call `install_code`, `reinstall_code`, or `upgrade` with a gzip-compressed WASM. Crafting a valid gzip header (`\x1f\x8b\x08`) with a large uncompressed size in the footer and corrupted body data is trivial. The attacker only needs to control a canister (free to create) and submit repeated `install_code` ingress messages. The ingress message size limit (2 MB) bounds the compressed payload but not the decompression work, since the decompressor reads up to `uncompressed_size` bytes from the stream before failing.

### Recommendation

- **Short term**: Move the `wasm_execution_mode(context.wasm_source.clone())` call to **after** `prepay_execution_cycles`, or charge a flat fee proportional to the compressed WASM size before any decompression is attempted. Alternatively, use only the gzip footer size (already available from `decoded_wasm_size`, which does not decompress) to determine the execution mode, deferring actual decompression to the sandbox where it is metered.
- **Long term**: Audit all code paths in the replica process that decompress or parse attacker-supplied WASM bytes before cycles are charged. Ensure that any expensive operation on attacker-controlled data is gated behind a prepaid resource check.

### Proof of Concept

1. Create a canister (attacker controls it as controller).
2. Craft a byte sequence: valid gzip magic bytes `\x1f\x8b\x08`, arbitrary header, ~2 MB of random/corrupted compressed data, and a footer with `uncompressed_size` set to `wasm_max_size - 1` (e.g., 99 MB).
3. Submit `install_code` with this crafted blob as the WASM module.
4. In `install_code_dts`, `wasm_execution_mode` calls `decode_wasm`, which reads the footer, sees a 99 MB uncompressed size (within limit), creates a `libflate::gzip::Decoder`, and attempts to stream up to 99 MB + 1 bytes through the decompressor. The corrupted body causes a decompression error after significant CPU work.
5. `wasm_execution_mode` silently returns `Wasm32`. Execution proceeds to `prepay_execution_cycles`.
6. The prepay succeeds (canister has cycles), but the decompression work was already done for free.
7. Repeat in a tight loop. Each call forces ~99 MB of decompression work in the replica process with no net cycles cost to the attacker (the prepaid cycles are refunded since no instructions are consumed before the eventual compilation failure). [5](#0-4) [6](#0-5)

### Citations

**File:** rs/execution_environment/src/canister_manager.rs (L861-892)
```rust
        let wasm_execution_mode = wasm_execution_mode(context.wasm_source.clone());

        let prepaid_execution_cycles = match prepaid_execution_cycles {
            Some(prepaid_execution_cycles) => prepaid_execution_cycles,
            None => {
                let memory_usage = canister.memory_usage();
                let message_memory_usage = canister.message_memory_usage();
                let reveal_top_up = canister.controllers().contains(message.sender());

                match self.cycles_account_manager.prepay_execution_cycles(
                    &mut canister.system_state,
                    memory_usage,
                    message_memory_usage,
                    execution_parameters.compute_allocation,
                    execution_parameters.instruction_limits.message(),
                    subnet_cycles_config,
                    reveal_top_up,
                    wasm_execution_mode,
                ) {
                    Ok(cycles) => cycles,
                    Err(err) => {
                        return DtsInstallCodeResult::Finished {
                            canister,
                            message,
                            call_id,
                            instructions_used: NumInstructions::from(0),
                            result: Err(CanisterManagerError::InstallCodeNotEnoughCycles(err)),
                        };
                    }
                }
            }
        };
```

**File:** rs/execution_environment/src/canister_manager.rs (L3143-3159)
```rust
pub fn wasm_execution_mode(wasm_module_source: WasmSource) -> WasmExecutionMode {
    let wasm_module = match wasm_module_source.into_canister_module() {
        Ok(wasm_module) => wasm_module,
        Err(_err) => {
            return WasmExecutionMode::Wasm32;
        }
    };

    let decoded_wasm_module = match decode_wasm(
        EmbeddersConfig::new().wasm_max_size,
        Arc::new(wasm_module.as_slice().to_vec()),
    ) {
        Ok(decoded_wasm_module) => decoded_wasm_module,
        Err(_err) => {
            return WasmExecutionMode::Wasm32;
        }
    };
```

**File:** rs/embedders/src/wasm_utils/decoding.rs (L63-110)
```rust
pub fn decode_wasm(
    max_size: NumBytes,
    module: Arc<Vec<u8>>,
) -> Result<BinaryEncodedWasm, WasmValidationError> {
    let module_bytes = module.as_slice();
    let (encoding, uncompressed_size) = wasm_encoding_and_size(module_bytes)?;
    if uncompressed_size as u64 > max_size.get() {
        return Err(WasmValidationError::ModuleTooLarge {
            size: uncompressed_size as u64,
            allowed: max_size.get(),
        });
    }

    match encoding {
        WasmEncoding::Wasm => Ok(BinaryEncodedWasm::new_shared(module)),
        WasmEncoding::Gzip => {
            let decoder = libflate::gzip::Decoder::new(module_bytes).map_err(|e| {
                WasmValidationError::DecodingError(format!(
                    "failed to decode compressed Wasm module: {e}"
                ))
            })?;

            let mut buf = Vec::with_capacity(uncompressed_size);
            // We cannot trust that the uncompressed size is set correctly.
            // Even if the size bytes are correct, they are only size modulo
            // 2^32.  To handle gzip bombs gracefully, we don't read more than
            // the uncompressed size from the uncompressed stream. We've already
            // checked that the uncompressed size is less than the maximum
            // module size.
            decoder
                .take(uncompressed_size as u64 + 1)
                .read_to_end(&mut buf)
                .map_err(|e| {
                    WasmValidationError::DecodingError(format!(
                        "failed to decode compressed Wasm module: {e}"
                    ))
                })?;

            if buf.len() != uncompressed_size {
                return Err(WasmValidationError::DecodingError(format!(
                    "specified uncompressed size {} does not match extracted size {}",
                    uncompressed_size,
                    buf.len()
                )));
            }
            Ok(BinaryEncodedWasm::new(buf))
        }
    }
```

**File:** rs/execution_environment/src/canister_manager/types.rs (L165-174)
```rust
    pub fn instructions_to_assemble(&self) -> NumInstructions {
        match self {
            Self::CanisterModule(_module) => NumInstructions::from(0),
            // Charge one instruction per byte, assuming each chunk is the
            // maximum size.
            Self::ChunkStore {
                chunk_hashes_list, ..
            } => NumInstructions::from((chunk_size() * chunk_hashes_list.len() as u64).get()),
        }
    }
```

**File:** rs/execution_environment/src/hypervisor.rs (L155-167)
```rust
        let wasm_size_result = decoded_wasm_size(canister_module.as_slice());
        let wasm_size = match wasm_size_result {
            Ok(size) => std::cmp::max(size, canister_module.len()),
            Err(_) => canister_module.len(),
        };
        let compilation_cost = self.cost_to_compile_wasm_instruction * wasm_size as u64;
        if let Err(err) = wasm_size_result {
            let total_cost = self.create_execution_state_base_cost + compilation_cost;
            round_limits.instructions -= as_round_instructions(total_cost);
            self.compilation_cache
                .insert_err(&canister_module, err.clone().into());
            return (total_cost, Err(err.into()));
        }
```
