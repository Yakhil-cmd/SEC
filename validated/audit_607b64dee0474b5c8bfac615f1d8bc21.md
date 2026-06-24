Audit Report

## Title
UTF-8 Boundary Panic in `method_name` Truncation Causes Request Handler Crash — (`rs/boundary_node/ic_boundary/src/http/middleware/process.rs`, `src/metrics.rs`)

## Summary
Two locations in the boundary node truncate `method_name` using raw byte-index slicing (`&s[..n]`) without verifying the index falls on a UTF-8 codepoint boundary. Any unauthenticated API caller can craft a CBOR request whose `method_name` has a multi-byte character straddling the truncation boundary, triggering a Rust panic. The panic in `postprocess_response` resets the connection; the panic in the spawned metrics task silently drops all metrics and log entries for that request.

## Finding Description
`MAX_LOGGING_METHOD_NAME_LENGTH` is defined in `metrics.rs` and imported into `process.rs` [1](#0-0)  as a byte count. Two sites perform raw byte-index slicing:

- `process.rs` line 264: [2](#0-1) 
- `metrics.rs` lines 627–630: [3](#0-2) 

The upstream guard `check_method_name_length` only enforces `MAX_METHOD_NAME_LENGTH` (20,000 bytes) [4](#0-3)  — it does not prevent multi-byte characters from straddling the logging truncation boundary. A `String` deserialized from CBOR is valid UTF-8, but Rust's `str` indexing panics when the byte offset does not lie on a codepoint boundary. A method name of 49 ASCII bytes followed by `'é'` (U+00E9, 2 bytes, total 51 bytes) causes `truncated_len = 51.min(N) = N` to land at byte `N` inside the second byte of `'é'`, triggering the panic.

## Impact Explanation
The panic in `postprocess_response` (process.rs line 264) unwinds the axum/tower request handler task, causing a connection reset visible to the client. This is repeatable by any unauthenticated caller on any `/api/v2/canister/{id}/call`, `/query`, or `/read_state` endpoint, constituting a targeted application-level DoS against boundary node connections. This matches the **High** impact class: *Application/platform-level DoS, crash, or boundary/API security impact with concrete user or protocol harm* ($2,000–$10,000). The panic in the `tokio::spawn` task (metrics.rs line 629) is caught by Tokio and results in silent loss of Prometheus metrics and structured logs for affected requests. [5](#0-4) 

## Likelihood Explanation
No authentication, no privileged role, and no network-level capability is required. The exploit is reachable through the standard public IC API with a single crafted CBOR envelope. It is deterministically reproducible and can be repeated indefinitely against any boundary node.

## Recommendation
Replace raw byte-index truncation with character-boundary-aware truncation at both sites:

```rust
// Instead of: &name[..name.len().min(MAX_LOGGING_METHOD_NAME_LENGTH)]
let truncated: String = name.chars().take(MAX_LOGGING_METHOD_NAME_LENGTH).collect();
```

Or use `floor_char_boundary` (stable since Rust 1.86) if byte-length semantics must be preserved:
```rust
let end = name.floor_char_boundary(MAX_LOGGING_METHOD_NAME_LENGTH);
let truncated = &name[..end];
```

Apply the fix to both `process.rs` line 264 and `metrics.rs` lines 628–629.

## Proof of Concept
```rust
// method_name = 49 × 'a' + 'é' (U+00E9 = 0xC3 0xA9) → 51 bytes total
let method_name = "a".repeat(49) + "é";
assert_eq!(method_name.len(), 51);
let truncated_len = method_name.len().min(50); // = 50
let _ = &method_name[..truncated_len]; // panics: byte 50 is not a char boundary
```

Send this as the `method_name` field in a CBOR-encoded `ICRequestEnvelope` to any `/api/v2/canister/{id}/call` endpoint. The boundary node's `postprocess_response` middleware will panic, resetting the connection. Adjust the ASCII prefix length to match `MAX_LOGGING_METHOD_NAME_LENGTH - 1` to ensure the multi-byte character straddles the boundary.

### Citations

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L18-18)
```rust
    metrics::MAX_LOGGING_METHOD_NAME_LENGTH,
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L50-66)
```rust
/// Restrict the method name to its max length
pub const MAX_METHOD_NAME_LENGTH: usize = 20_000;

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
