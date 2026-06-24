The code is confirmed. Let me analyze the exact mechanics.

**The truncation logic in both locations:**

`metrics.rs` lines 627-630 (inside `tokio::spawn` at line 546):
```rust
let method_name = ctx.method_name.as_ref().map(|name| {
    let truncated_len = name.len().min(MAX_LOGGING_METHOD_NAME_LENGTH);
    name[..truncated_len].to_string()  // byte-index slice on a &str
});
```

`process.rs` lines 263-264 (in `postprocess_response`):
```rust
ctx.method_name.as_ref().and_then(|v| {
    let truncated = &v[..v.len().min(MAX_LOGGING_METHOD_NAME_LENGTH)];
```

**The core issue:** In Rust, `&str[..n]` panics at runtime if `n` is a byte offset that falls inside a multi-byte UTF-8 codepoint. `MAX_LOGGING_METHOD_NAME_LENGTH` is a **byte count** (50), not a character count. A `String` deserialized from CBOR is guaranteed valid UTF-8, but slicing at a byte boundary that splits a multi-byte character still panics.

**Concrete trigger:** A method name of 49 ASCII bytes + `'é'` (U+00E9, encoded as `0xC3 0xA9`, 2 bytes) = 51 total bytes. `check_method_name_length` passes (51 ≤ 20,000). Then `truncated_len = 51.min(50) = 50`, and `name[..50]` attempts to slice at byte 50, which is `0xA9` — the second byte of `'é'`, not a valid UTF-8 boundary → **panic**.

**Impact per location:**
- `process.rs` line 264: panic in `postprocess_response` middleware unwinds the axum request handler task → connection reset for the client. Repeatable by any unauthenticated caller.
- `metrics.rs` line 629: panic inside the `tokio::spawn` task (line 546) — Tokio catches it, the task is dropped silently, metrics and structured logs for that request are lost.

---

### Title
UTF-8 Boundary Panic in `method_name` Truncation Causes Request Handler Crash and Silent Metric Loss — (`rs/boundary_node/ic_boundary/src/http/middleware/process.rs`, `src/metrics.rs`)

### Summary
Two locations truncate `method_name` using a raw byte-index slice (`&s[..50]`) without checking that the index falls on a UTF-8 character boundary. Any unprivileged API client can send a CBOR request whose `method_name` straddles that boundary, triggering a Rust panic.

### Finding Description
`MAX_LOGGING_METHOD_NAME_LENGTH` is defined as a byte count of 50. [1](#0-0) [2](#0-1) 

Both sites compute `truncated_len = name.len().min(50)` and then index `name[..truncated_len]`. Rust's `str` indexing panics when the byte offset does not lie on a codepoint boundary. The upstream guard `check_method_name_length` only enforces a maximum byte length of 20,000; it does not prevent multi-byte characters from straddling byte 50. [3](#0-2) 

The `method_name` field is deserialized from a CBOR text string, which is valid UTF-8 by the CBOR spec, so `serde_cbor` will accept it as a Rust `String`. The validity of the overall string does not prevent a panic when slicing at an interior byte of a multi-byte character.

### Impact Explanation
- **`postprocess_response` (process.rs line 264):** The panic unwinds the axum/tower request handler task, causing a connection reset. This is repeatable by any unauthenticated caller on any `/api/v2/canister/{id}/call`, `/query`, or `/read_state` endpoint, constituting a targeted DoS against individual boundary node connections.
- **`metrics_middleware` spawned task (metrics.rs line 629):** The panic is caught by Tokio's task runtime; the spawned task is silently dropped, losing all Prometheus counter increments and structured log entries for that request. [4](#0-3) 

### Likelihood Explanation
The exploit requires no authentication, no privileged role, and no network-level attack. It is reachable through the standard public IC API. A single crafted CBOR envelope is sufficient to reproduce it locally.

### Recommendation
Replace raw byte-index truncation with a character-boundary-aware truncation, e.g.:

```rust
let truncated_len = name
    .char_indices()
    .map(|(i, _)| i)
    .take_while(|&i| i < MAX_LOGGING_METHOD_NAME_LENGTH)
    .last()
    .map(|i| i)
    .unwrap_or(0);
// or simply:
let truncated: String = name.chars().take(MAX_LOGGING_METHOD_NAME_LENGTH).collect();
```

Apply the fix to both `process.rs` line 264 and `metrics.rs` line 628-629.

### Proof of Concept
```rust
// method_name = 49 × 'a' + 'é' (U+00E9 = 0xC3 0xA9) → 51 bytes total
let method_name = "a".repeat(49) + "é";
assert_eq!(method_name.len(), 51);
// Simulates the truncation:
let truncated_len = method_name.len().min(50); // = 50
// The following panics: byte 50 is 0xA9, the second byte of 'é'
let _ = &method_name[..truncated_len]; // thread 'main' panicked: byte index 50 is not a char boundary
```

Send this as the `method_name` field in a CBOR-encoded `ICRequestEnvelope` to any `/api/v2/canister/{id}/call` endpoint. The boundary node's `postprocess_response` middleware will panic, resetting the connection.

### Citations

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L53-66)
```rust
fn check_method_name_length<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let s: Option<String> = Option::<String>::deserialize(deserializer)?;
    if let Some(val) = &s
        && val.len() > MAX_METHOD_NAME_LENGTH
    {
        return Err(D::Error::custom(format!(
            "Method name exceeds maximum allowed length of {MAX_METHOD_NAME_LENGTH}"
        )));
    }

    Ok(s)
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L263-264)
```rust
        ctx.method_name.as_ref().and_then(|v| {
            let truncated = &v[..v.len().min(MAX_LOGGING_METHOD_NAME_LENGTH)];
```

**File:** rs/boundary_node/ic_boundary/src/metrics.rs (L546-548)
```rust
    tokio::spawn(async move {
        // Wait for the streaming to finish
        let response_size = rx.await.unwrap_or(Ok(0)).unwrap_or(0);
```

**File:** rs/boundary_node/ic_boundary/src/metrics.rs (L627-630)
```rust
        let method_name = ctx.method_name.as_ref().map(|name| {
            let truncated_len = name.len().min(MAX_LOGGING_METHOD_NAME_LENGTH);
            name[..truncated_len].to_string()
        });
```
