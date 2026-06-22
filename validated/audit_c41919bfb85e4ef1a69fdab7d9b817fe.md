The code is confirmed. Let me verify the exact slice operation and its Rust semantics.

At [1](#0-0)  the operation is:

```rust
ctx.method_name.as_ref().and_then(|v| {
    let truncated = &v[..v.len().min(MAX_LOGGING_METHOD_NAME_LENGTH)];
```

`v` is a `&String`. In Rust, `&v[..50]` is a **byte-index slice on a `str`**. If byte 50 falls inside a multi-byte UTF-8 codepoint, this panics with `byte index N is not a char boundary`.

`MAX_LOGGING_METHOD_NAME_LENGTH` is confirmed as `50` at [2](#0-1) .

The only upstream guard is `check_method_name_length` at [3](#0-2) , which only rejects strings longer than `MAX_METHOD_NAME_LENGTH` (20,000 bytes). A 52-byte string (49 ASCII bytes + one 3-byte CJK character) passes this check trivially.

---

### Title
UTF-8 Char-Boundary Panic in `postprocess_response` via Crafted `method_name` — (`rs/boundary_node/ic_boundary/src/http/middleware/process.rs`)

### Summary
An unprivileged attacker can send a valid CBOR-encoded IC request whose `method_name` contains a multi-byte UTF-8 character spanning byte offset 50. The `postprocess_response` middleware slices the string at byte 50 without a char-boundary check, causing a Rust panic.

### Finding Description
`postprocess_response` truncates `method_name` for logging/header purposes using a raw byte-index slice:

```rust
// process.rs:264
let truncated = &v[..v.len().min(MAX_LOGGING_METHOD_NAME_LENGTH)];
```

`MAX_LOGGING_METHOD_NAME_LENGTH` is `50`. If `v` is, e.g., 49 ASCII bytes followed by a 3-byte CJK character (total 52 bytes), then `v.len().min(50)` = `50`, and `&v[..50]` attempts to slice at byte 50, which is the second byte of the 3-byte codepoint. Rust's `str` indexing panics unconditionally when the index is not on a UTF-8 character boundary.

The upstream validator `check_method_name_length` only rejects strings exceeding 20,000 bytes — a 52-byte crafted string passes without issue. [3](#0-2) 

### Impact Explanation
In Axum/tokio, a panic inside a spawned connection-handling task is caught by the tokio runtime at the task boundary — the task is aborted and the connection is dropped, but **the process itself does not crash**. The actual impact is: the specific request/connection fails (connection reset or no response), and the panic is logged. An attacker can reliably trigger this for any request they control, causing targeted request-level denial of service. The process continues serving other connections normally.

The claim of "crashing the boundary-node process" in the question is overstated — tokio catches task panics by default. The real impact is a per-request panic and connection abort for any request with a crafted `method_name`.

### Likelihood Explanation
The attack requires only a valid CBOR envelope with a `method_name` of 49 ASCII bytes + one multi-byte UTF-8 character. No authentication, no special privileges, no volumetric component. Any client that can POST to `/api/v2/canister/{id}/query` can trigger it.

### Recommendation
Replace the raw byte-index slice with a char-boundary-safe truncation:

```rust
// Safe: truncate to at most MAX_LOGGING_METHOD_NAME_LENGTH bytes at a char boundary
let end = v.floor_char_boundary(MAX_LOGGING_METHOD_NAME_LENGTH); // Rust 1.73+ nightly
let truncated = &v[..end];
```

Or on stable Rust:
```rust
let truncated = v.char_indices()
    .take_while(|(i, _)| *i < MAX_LOGGING_METHOD_NAME_LENGTH)
    .last()
    .map(|(i, c)| &v[..i + c.len_utf8()])
    .unwrap_or("");
```

### Proof of Concept
```rust
use std::panic::catch_unwind;

const MAX_LOGGING_METHOD_NAME_LENGTH: usize = 50;

fn truncate(v: &str) -> &str {
    &v[..v.len().min(MAX_LOGGING_METHOD_NAME_LENGTH)]
}

fn main() {
    // 49 ASCII bytes + one 3-byte CJK character = 52 bytes total
    let method_name = "a".repeat(49) + "中"; // "中" is 3 bytes: 0xE4 0xB8 0xAD
    assert_eq!(method_name.len(), 52);

    // Panics: byte 50 is inside the 3-byte sequence for "中"
    let result = catch_unwind(|| truncate(&method_name));
    assert!(result.is_err(), "Expected panic at non-char-boundary");
    println!("Panic confirmed: byte-index slice at offset 50 is not a char boundary");
}
```

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

**File:** rs/boundary_node/ic_boundary/src/metrics.rs (L61-61)
```rust
pub(crate) const MAX_LOGGING_METHOD_NAME_LENGTH: usize = 50;
```
