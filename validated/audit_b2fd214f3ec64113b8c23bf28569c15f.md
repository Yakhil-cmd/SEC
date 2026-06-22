Now I have the full code. Let me analyze the claimed vulnerability precisely.

Now I have everything needed for a complete analysis. Let me trace the exact code path.

**Step 1 — The size check in `RequestInPrep::new`:**

The check at line 101 uses the **raw byte count** `method_name_len` from the Wasm heap:

```rust
if method_name_len as u64 > max_size_local_subnet.get() {  // raw bytes
    return Err(...)
}
// ...
String::from_utf8_lossy(method_name).to_string()  // may expand up to 3×
```

`from_utf8_lossy` replaces each invalid byte with U+FFFD (3 UTF-8 bytes). A `method_name_len` of `max_size_local_subnet / 3 + 1` bytes of `0x80` passes the raw check but stores a `method_name` whose `.len()` is `> max_size_local_subnet`.

**Step 2 — The subtraction in `extend_method_payload` line 169:**

```rust
let current_size = self.method_name.len() + self.method_payload.len();
// current_size = ~10,485,762 (> max_size_local_subnet = 10,485,760)
if size as u64 > max_size_local_subnet.get() - current_size as u64 {
//                 10,485,760u64 - 10,485,762u64  ← wraps in release mode
```

**Step 3 — Cargo.toml release profile:**

The workspace `[profile.release]` only sets `debug = true` — **no `overflow-checks = true`**. In release mode, Rust's `u64` subtraction wraps silently. The result is `~u64::MAX - 1`, making the guard `size as u64 > ~u64::MAX` permanently false for any realistic `size`.

**Step 4 — Secondary check in `into_output_request` line 232:**

```rust
if payload_size > max_size_local_subnet.get() {
    return Err(...)  // fires at ic0_call_perform time
}
```

This catches the oversized payload, so no oversized message is actually dispatched. However, the `method_payload` Vec on the sandbox process heap grows unboundedly (bounded only by instruction limits and sandbox memory) **before** this check fires.

**Step 5 — The `max_sum_exported_function_name_lengths` gate:**

The first check at line 89 requires `method_name_len <= max_sum_exported_function_name_lengths`. The attacker controls their own Wasm module and can export functions with names totaling `> max_size_local_subnet / 3` bytes. With a Wasm module size limit of ~100MB, exporting ~3.5MB of function names is feasible.

---

### Title
Integer Underflow in `extend_method_payload` Bypasses Payload Size Guard via UTF-8 Expansion of `method_name` — (`rs/embedders/src/wasmtime_embedder/system_api/request_in_prep.rs`)

### Summary

`RequestInPrep::new` validates `method_name` length using the **raw byte count** from the Wasm heap, then stores the result of `String::from_utf8_lossy`, which can expand invalid UTF-8 bytes up to 3×. The stored `method_name.len()` can therefore exceed `max_size_local_subnet`. In `extend_method_payload`, the guard `max_size_local_subnet.get() - current_size as u64` wraps to `~u64::MAX` in release builds (no `overflow-checks`), disabling the per-append size check entirely and allowing `method_payload` to grow beyond the intended limit before `into_output_request`'s secondary check fires.

### Finding Description

In `RequestInPrep::new`: [1](#0-0) 

The guard checks `method_name_len` (raw bytes), but the stored value is the result of `from_utf8_lossy`, which replaces each invalid byte with the 3-byte U+FFFD sequence. An attacker supplying `N = max_size_local_subnet / 3 + 1` bytes of `0x80` passes the raw check (`N ≤ max_size_local_subnet`) but stores a `method_name` of length `3N > max_size_local_subnet`.

In `extend_method_payload`: [2](#0-1) 

`current_size` now exceeds `max_size_local_subnet.get()`. The subtraction `max_size_local_subnet.get() - current_size as u64` is a plain `u64 - u64` with no saturation or checked arithmetic. The workspace release profile: [3](#0-2) 

does not set `overflow-checks = true`, so in release builds the subtraction wraps to `~u64::MAX`. The guard `size as u64 > ~u64::MAX` is always false, and every subsequent `ic0.call_data_append` call appends data without restriction.

The secondary check in `into_output_request` does catch the oversized payload at `ic0_call_perform` time: [4](#0-3) 

but the `method_payload` Vec has already been allocated on the sandbox process heap.

### Impact Explanation

An unprivileged canister can cause the Wasm sandbox process to allocate heap memory far beyond `MAX_INTER_CANISTER_PAYLOAD_IN_BYTES` (2 MB) or `max_size_local_subnet` (10 MB). The total allocation is bounded by the per-message instruction limit and sandbox memory limits, not by the intended payload size guard. This can cause sandbox OOM, crashing the sandbox process for that canister's message. The sandbox is isolated from the replica, so consensus is not directly affected, but the canister's execution is disrupted and the invariant that payload size is bounded at construction time is violated.

### Likelihood Explanation

The attack requires:
1. A canister Wasm module with exported function names totaling `> max_size_local_subnet / 3 ≈ 3.5 MB` (feasible within the ~100 MB Wasm module size limit).
2. Calling `ic0.call_new` with `method_name_src` pointing to `~3.5 MB` of `0x80` bytes in the Wasm heap.
3. Calling `ic0.call_data_append` repeatedly.

This is fully within the capability of an unprivileged canister developer and requires no privileged access.

### Recommendation

Replace the plain subtraction with a saturating or checked operation:

```rust
// Option A: saturating subtraction (guard fires immediately if already over limit)
let remaining = max_size_local_subnet.get().saturating_sub(current_size as u64);
if size as u64 > remaining { ... }

// Option B: checked subtraction
let remaining = max_size_local_subnet.get().checked_sub(current_size as u64).unwrap_or(0);
if size as u64 > remaining { ... }
```

Additionally, validate `method_name.len()` (the post-`from_utf8_lossy` length) against `max_size_local_subnet` inside `RequestInPrep::new`, not just the raw `method_name_len`.

### Proof of Concept

```rust
// In a unit test (release mode, no overflow-checks):
let max_size_remote = NumBytes::from(2 * 1024 * 1024); // 2 MB
let multiplier = 5u64;
let max_size_local = max_size_remote * multiplier; // 10,485,760

// method_name_len = 3,495,254 bytes of 0x80
// Raw check: 3,495,254 <= 10,485,760 → passes
// After from_utf8_lossy: 3,495,254 × 3 = 10,485,762 > 10,485,760
let method_name_len = (max_size_local.get() / 3 + 2) as usize;
let mut heap = vec![0u8; method_name_len + 100];
for b in &mut heap[0..method_name_len] { *b = 0x80; }

let mut req = RequestInPrep::new(
    sender, 0, 1, 0, method_name_len, &heap,
    cb.clone(), cb, max_size_remote, multiplier,
    method_name_len + 1, // max_sum_exported_function_name_lengths
).unwrap(); // succeeds — raw check passes

// In release mode: current_size = 10,485,762 > max_size_local = 10,485,760
// Subtraction wraps: 10,485,760 - 10,485,762 = u64::MAX - 1
// Guard: 1 > u64::MAX - 1 → false → append succeeds
req.extend_method_payload(0, 1, &heap).unwrap(); // should fail but doesn't
```

### Citations

**File:** rs/embedders/src/wasmtime_embedder/system_api/request_in_prep.rs (L99-116)
```rust
            // method_name checked against payload on the call.
            let max_size_local_subnet = max_size_remote_subnet * multiplier_max_size_local_subnet;
            if method_name_len as u64 > max_size_local_subnet.get() {
                return Err(HypervisorError::UserContractViolation {
                    error: format!(
                        "Size of method_name {method_name_len} exceeds the allowed limit of {max_size_local_subnet}."
                    ),
                    suggestion: LARGE_NAME_SUGGESTION.to_string(),
                    doc_link: doc_ref(LARGE_NAME_LINK),
                });
            }
            let method_name = valid_subslice(
                "ic0.call_new method_name",
                InternalAddress::new(method_name_src),
                InternalAddress::new(method_name_len),
                heap,
            )?;
            String::from_utf8_lossy(method_name).to_string()
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/request_in_prep.rs (L166-169)
```rust
        let current_size = self.method_name.len() + self.method_payload.len();
        let max_size_local_subnet =
            self.max_size_remote_subnet * self.multiplier_max_size_local_subnet;
        if size as u64 > max_size_local_subnet.get() - current_size as u64 {
```

**File:** rs/embedders/src/wasmtime_embedder/system_api/request_in_prep.rs (L229-240)
```rust
    let payload_size = (method_name.len() + method_payload.len()) as u64;
    {
        let max_size_local_subnet = max_size_remote_subnet * multiplier_max_size_local_subnet;
        if payload_size > max_size_local_subnet.get() {
            return Err(HypervisorError::UserContractViolation {
                error: format!(
                    "Request to {destination_canister}:{method_name} has a payload size of {payload_size}, which exceeds the allowed limit of {max_size_remote_subnet}."
                ),
                suggestion: PAYLOAD_SIZE_SUGGESTION.to_string(),
                doc_link: doc_ref(PAYLOAD_SIZE_LINK),
            });
        }
```

**File:** Cargo.toml (L555-559)
```text
[profile.release]
# Add debug information to the release build (does NOT reduce the level of optimization!)
# Makes flamegraphs more readable.
# https://doc.rust-lang.org/cargo/reference/manifest.html#the-profile-sections
debug = true
```
