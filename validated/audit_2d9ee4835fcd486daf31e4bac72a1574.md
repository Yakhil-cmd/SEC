Audit Report

## Title
Unbounded O(N) Heap Allocation in `stream_slice_partial_tree` via Crafted XNet `witness_begin`/`msg_begin` Parameters — (`rs/state_manager/src/stream_encoding.rs`)

## Summary
The XNet HTTP endpoint accepts `witness_begin` and `msg_begin` query parameters with no bounds on their spread. By setting `witness_begin = stream.messages_begin()` and `msg_begin = stream.messages_end()`, a registered IC node can cause `stream_slice_partial_tree` to allocate a `Vec` proportional to the full message count of the stream. The `byte_limit` parameter does not constrain this allocation path, enabling repeated O(N) heap exhaustion per request.

## Finding Description
**Entry point** (`rs/http_endpoints/xnet/src/lib.rs`, lines 383–414): The XNet endpoint parses `witness_begin` and `msg_begin` as raw `u64` values and passes them directly to `encode_certified_stream_slice` with no application-level spread check.

**Validation** (`rs/state_manager/src/lib.rs`, lines 4052–4061): `validate_slice_begin` uses the condition `stream.messages_end() < begin` (strict less-than). This means `begin = stream.messages_end()` passes validation. Both `witness_begin = stream.messages_begin()` (B) and `msg_begin = stream.messages_end()` (E) are accepted.

**`to` computation** (lines 4067–4070): With `msg_from = E` and no `msg_limit`, `to = stream.messages_end() = E`.

**`encode_stream_slice`** (`rs/state_manager/src/stream_encoding.rs`, lines 120–134): Called with `from = to = E`. `actual_to` is initialized to `from` (line 120) and no messages are traversed, so it returns `actual_to = E`.

**O(N) allocation** (lines 148–153): `stream_slice_partial_tree` is called with `from = B`, `to = E`. Since `to != from`, it executes:
```rust
let mut messages = Vec::with_capacity((to - from).get() as usize);
for i in from.get()..to.get() {
    messages.push((i.to_label(), empty_leaf.clone()));
}
```
This allocates and populates a `Vec` of `E - B` entries. The `byte_limit` parameter is only consulted inside `encode_stream_slice` (line 84), never in `stream_slice_partial_tree`. There is no cap on `to - witness_from` anywhere in the call chain.

## Impact Explanation
Each crafted request causes heap allocation proportional to the full stream message count, completely independent of `byte_limit`. With `XNET_ENDPOINT_MAX_CONCURRENT_REQUESTS = 4` (`rs/http_endpoints/xnet/src/lib.rs`, line 54), an attacker can sustain 4 simultaneous O(N) allocations. For a stream with hundreds of thousands of messages, this can exhaust available heap memory on the serving replica, causing OOM or severe allocator pressure, degrading or halting the replica's ability to participate in consensus. This matches the allowed High impact: **Application/platform-level DoS, crash, consensus blocking, or subnet availability impact not based on raw volumetric DDoS**.

## Likelihood Explanation
The attacker must be a registered IC node, as the XNet endpoint uses mTLS with `SomeOrAllNodes::All` (`rs/http_endpoints/xnet/src/lib.rs`, line 261). This is not accessible to anonymous internet users. However, within the IC threat model, a single malicious or compromised node below the consensus fault threshold is a valid attacker. The XNet endpoint is intentionally reachable by all other subnet replicas. The exploit requires only a single well-formed HTTP GET request with two specific query parameters — no brute force, no timing dependency, no state manipulation — and is trivially repeatable.

## Recommendation
1. **Cap `to - witness_from` before calling `stream_slice_partial_tree`**: Clamp `witness_from` so that `to - witness_from` is bounded by a function of `byte_limit` or a protocol-defined constant (e.g., the same `MAX_STREAM_MESSAGES` used elsewhere).
2. **Strict upper bound validation**: Change `stream.messages_end() < begin` to `stream.messages_end() <= begin` (or `begin >= stream.messages_end()` → reject) so that `msg_begin = stream.messages_end()` is rejected as an empty-payload request.
3. **Explicit spread limit**: Reject or clamp requests where `msg_begin - witness_begin` exceeds a protocol-defined maximum before any allocation occurs.

## Proof of Concept
```
GET /api/v1/stream/<SUBNET_ID>?witness_begin=<stream.messages_begin()>&msg_begin=<stream.messages_end()>
```
Trace:
- `witness_from = B` → passes `validate_slice_begin` (B is within `[B, E]`)
- `msg_from = E` → passes `validate_slice_begin` (non-strict: `E < E` is false)
- `to = E` (no `msg_limit`)
- `encode_stream_slice(from=E, to=E)` → `actual_to = E` (empty range, no messages)
- `stream_slice_partial_tree(subnet, from=B, to=E)` → `Vec::with_capacity(E - B)` + loop of `E - B` iterations
- For `E - B = 500,000`: ~500,000 heap entries allocated, unbounded by any `byte_limit`

A deterministic integration test can be written using `PocketIC` or the existing state manager test harness: construct a stream with N messages, call `encode_certified_stream_slice` with `witness_begin = messages_begin()` and `msg_begin = messages_end()`, and assert that peak heap allocation is bounded (currently it is not).