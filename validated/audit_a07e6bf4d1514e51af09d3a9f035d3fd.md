Audit Report

## Title
Attacker-Controlled Gzip Footer Inflates Compilation Cost Charged in Error Path, Draining Round Instruction Limit - (File: rs/embedders/src/wasm_utils/decoding.rs, rs/execution_environment/src/hypervisor.rs)

## Summary
`wasm_encoding_and_size()` reads the uncompressed size from the gzip footer — a 4-byte field fully controlled by the submitter — and returns it as a trusted `Ok(size)`. `Hypervisor::create_execution_state()` uses this value to compute a `compilation_cost` estimate that is charged against the shared `round_limits.instructions` budget in the error branch when `wasm_executor.create_execution_state()` fails with `ModuleTooLarge`. An unprivileged canister developer can craft a gzip Wasm with footer bytes set to `u32::MAX`, causing ~25.7 trillion instructions (`6_000 * 4,294,967,295`) to be deducted from the round budget, starving all other canisters scheduled in the same execution round.

## Finding Description

**Root cause — `wasm_encoding_and_size()` in `rs/embedders/src/wasm_utils/decoding.rs` (lines 37–41):**

The function reads the last 4 bytes of the gzip stream as a little-endian `u32` and returns it as `uncompressed_size` wrapped in `Ok`. The function carries an explicit warning that this value cannot be trusted. For a crafted module with footer `0xFF 0xFF 0xFF 0xFF`, it returns `Ok(4_294_967_295)`.

**Propagation — `Hypervisor::create_execution_state()` in `rs/execution_environment/src/hypervisor.rs` (lines 155–160):**

```rust
let wasm_size_result = decoded_wasm_size(canister_module.as_slice());
let wasm_size = match wasm_size_result {
    Ok(size) => std::cmp::max(size, canister_module.len()),
    Err(_) => canister_module.len(),
};
let compilation_cost = self.cost_to_compile_wasm_instruction * wasm_size as u64;
```

Because `decoded_wasm_size` returns `Ok(u32::MAX)` for the crafted module, `wasm_size_result` is `Ok`, so the early-return error guard at lines 161–167 is **not** triggered. `compilation_cost` is computed as `6_000 * 4_294_967_295 ≈ 25.7 trillion instructions` (using `DEFAULT_COST_TO_COMPILE_WASM_INSTRUCTION = 6_000` from `rs/config/src/embedders.rs` line 57–58).

**Error path trigger — `decode_wasm()` in `rs/embedders/src/wasm_utils/decoding.rs` (lines 69–73):**

Inside `wasm_executor.create_execution_state()`, `decode_wasm()` is called with `WASM_MAX_SIZE = 100 MiB`. Since `u32::MAX ≈ 4.3 GB > 100 MiB`, it returns `Err(WasmValidationError::ModuleTooLarge)`. This propagates as `Err(err)` from `wasm_executor.create_execution_state()`.

**Budget drain — `rs/execution_environment/src/hypervisor.rs` (lines 188–191):**

```rust
Err(err) => {
    let total_cost = self.create_execution_state_base_cost + compilation_cost;
    round_limits.instructions -= as_round_instructions(total_cost);
    (total_cost, Err(err))
}
```

The inflated `compilation_cost` (computed from the attacker-controlled footer) is charged against `round_limits.instructions`. On the success path (lines 183–185), the actual compiler-measured cost is used instead, so the manipulation is exclusive to the error path.

**Existing guards are insufficient:** The `decode_wasm()` size check correctly rejects the oversized module, but the `compilation_cost` estimate is computed and charged *before* this check's result is observed in the error branch. The comment in `decoded_wasm_size()` explicitly acknowledges the footer is untrustworthy, but the caller does not act on this warning.

## Impact Explanation

`round_limits.instructions` is a shared per-round budget across all canisters scheduled in the same execution round. A single crafted `install_code` call drains approximately 25.7 trillion instructions from this budget (with production defaults), which far exceeds any realistic per-round limit, preventing all other canisters in the round from executing. The attacker can repeat this across rounds. This constitutes a **subnet-level application/platform DoS** — all canisters on the subnet are starved of execution time — matching the High severity impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation

Any canister developer can submit a gzip-compressed Wasm via `install_code` or `install_chunked_code`. Crafting a gzip file with specific footer bytes is trivial (standard RFC 1952 format, achievable with any hex editor or a few lines of Python). No privileged access, governance majority, or threshold corruption is required. The attack requires only a canister the attacker controls and enough cycles to submit the `install_code` call itself (not the inflated compilation cost, which is charged after the round budget is already drained).

## Recommendation

1. **Do not use the gzip footer size for cost estimation.** In `create_execution_state`, replace the `Ok(size)` branch with `canister_module.len()` (the actual compressed size) when the module is gzip-encoded, or cap the estimated size at `wasm_max_size` before multiplying by `cost_to_compile_wasm_instruction`.
2. **Cap the size used for cost calculation** at `wasm_max_size` unconditionally before computing `compilation_cost`, so no footer value can produce a cost exceeding what a legitimately maximum-sized module would cost.
3. Alternatively, perform the `decode_wasm` size check **before** computing the compilation cost estimate, so the error path never charges based on the attacker-controlled footer value.

## Proof of Concept

1. Construct a minimal valid gzip stream: valid 10-byte header (`\x1f\x8b\x08` + flags/mtime/xfl/os), a valid DEFLATE-compressed empty or minimal Wasm body, and a crafted 8-byte footer where the last 4 bytes (ISIZE field) are `\xFF\xFF\xFF\xFF`.
2. Submit this as the `wasm_module` field of an `install_code` call to any canister the attacker controls.
3. `decoded_wasm_size()` returns `Ok(4_294_967_295)`.
4. `wasm_size = max(4_294_967_295, actual_compressed_len)` = `4_294_967_295`.
5. `compilation_cost = 6_000 * 4_294_967_295 = 25_769_803_770_000`.
6. `wasm_executor.create_execution_state()` calls `decode_wasm()`, which returns `ModuleTooLarge` (4.3 GB > 100 MiB).
7. Error branch executes: `round_limits.instructions -= as_round_instructions(base_cost + 25_769_803_770_000)`.
8. `round_limits.instructions` is drained, preventing all other canisters in the round from executing.

A deterministic integration test can be written using `PocketIC` or the existing test harness in `rs/execution_environment/tests/hypervisor.rs`: construct the crafted gzip bytes, call `hypervisor.create_execution_state()` with a `RoundLimits` initialized to a known instruction count, and assert that `round_limits.instructions` is reduced by the inflated amount after the call returns an error.