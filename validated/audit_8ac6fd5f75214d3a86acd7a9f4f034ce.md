Audit Report

## Title
Boundary Node DoS via CRLF in `method_name` Causing Panic in `postprocess_response` — (`rs/boundary_node/ic_boundary/src/http/middleware/process.rs`)

## Summary

The `check_method_name_length` deserializer only enforces a length bound on `method_name`, allowing arbitrary byte content including CRLF sequences to pass through. In `postprocess_response`, the attacker-controlled `method_name` is passed to `HeaderValue::from_maybe_shared(...).unwrap()`, which panics when the `http` crate rejects the CRLF bytes. No panic-catching layer exists in the middleware stack, so the panic aborts the Tokio connection handler task and drops the connection.

## Finding Description

**Validation gap in `check_method_name_length`:** The custom deserializer at [1](#0-0)  rejects names longer than `MAX_METHOD_NAME_LENGTH` (20,000 bytes) but performs zero content sanitization. A `method_name` of `"AAAAAAAAAA\r\nX-Evil: injected"` (30 bytes) passes unconditionally.

**Unsanitized value inserted into response header with `.unwrap()`:** In `postprocess_response`, the attacker-controlled string flows directly into: [2](#0-1) 

`Bytes::from(truncated.to_string())` creates a fresh `Bytes` object. `HeaderValue::from_maybe_shared` validates every byte when given a non-shared `Bytes`; bytes `\r` (0x0D) and `\n` (0x0A) are explicitly invalid per the HTTP/1.1 header value grammar enforced by the `http` crate. The function returns `Err(InvalidHeaderValue)`, and `.unwrap()` panics.

**No panic-catching layer in the middleware stack:** The `common_layers` `ServiceBuilder` chain at [3](#0-2)  includes no `CatchPanicLayer` or equivalent. A panic in `postprocess_response` propagates to the Tokio connection handler task, aborting it and dropping the connection.

The exploit path is: attacker sends CBOR-encoded IC API request → `preprocess_request` deserializes it, `check_method_name_length` passes (length < 20,000) → `method_name` stored in `RequestContext` → `postprocess_response` calls `HeaderValue::from_maybe_shared(Bytes::from("AAAAAAAAAA\r\nX-Evil: injected")).unwrap()` → panic → task abort.

## Impact Explanation

Each crafted request causes a task-level panic and connection drop on the boundary node. An attacker can send a continuous stream of such requests (no authentication required) to cause repeated panics across connection handler tasks, degrading or denying service to legitimate users. This matches the **High** impact class: *Application/platform-level DoS, crash, or boundary/API availability impact not based on raw volumetric DDoS* ($2,000–$10,000).

## Likelihood Explanation

- Requires no credentials, no privileged role, no key material.
- Entry point is any public IC API endpoint (`/api/v2/canister/.../call`, `/query`, `/read_state`).
- The CBOR body is trivially constructable; the CRLF must appear within the first `MAX_LOGGING_METHOD_NAME_LENGTH` bytes of `method_name`.
- No existing guard sanitizes or rejects non-printable bytes in `method_name` before header insertion.
- Reproducible locally with a single crafted request.

## Recommendation

Replace the bare `.unwrap()` at line 267 with explicit error handling. Either:

1. **Sanitize before insertion**: strip or percent-encode non-printable/non-ASCII bytes from `method_name` before constructing the `HeaderValue`.
2. **Handle the error gracefully**: use `if let Ok(v) = HeaderValue::from_maybe_shared(...) { response.headers_mut().insert(X_IC_METHOD_NAME, v); }` to silently skip insertion on invalid values rather than panicking.

The same `.unwrap()` pattern appears for `X_IC_ERROR_CAUSE` (line 203), `X_IC_CACHE_STATUS` (line 211), and `X_IC_CACHE_BYPASS_REASON` (line 217), but those values derive from internal `enum`-derived strings and are safe in practice. The `method_name` case is attacker-controlled and must be treated differently.

## Proof of Concept

```python
import cbor2, requests

method_name = "AAAAAAAAAA\r\nX-Evil: injected"  # 30 bytes, passes length check

envelope = {
    "content": {
        "request_type": "call",
        "sender": bytes(1),
        "canister_id": bytes(b'\x00' * 10),
        "method_name": method_name,
        "arg": b'',
        "ingress_expiry": 9999999999999999999,
        "nonce": b'\x01',
    }
}

body = cbor2.dumps(envelope)
resp = requests.post(
    "https://<boundary-node>/api/v2/canister/aaaaa-aa/call",
    data=body,
    headers={"Content-Type": "application/cbor"},
)
# Expected: connection reset / 500 due to panic in postprocess_response
print(resp.status_code)
```

A unit-level reproduction can be written directly against `postprocess_response` by constructing a mock `RequestContext` with `method_name = Some("AAAAAAAAAA\r\nX-Evil: injected".to_string())` and asserting that the middleware panics, confirming the bug without requiring a live boundary node.

### Citations

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L53-67)
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
}
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L263-269)
```rust
        ctx.method_name.as_ref().and_then(|v| {
            let truncated = &v[..v.len().min(MAX_LOGGING_METHOD_NAME_LENGTH)];
            response.headers_mut().insert(
                X_IC_METHOD_NAME,
                HeaderValue::from_maybe_shared(Bytes::from(truncated.to_string())).unwrap(),
            )
        });
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L1004-1013)
```rust
    let common_layers = ServiceBuilder::new()
        .layer(middleware_bouncer)
        .layer(middleware_geoip)
        .set_x_request_id(MakeRequestUuid)
        .layer(middleware_metrics)
        .layer(load_shedder_system_mw)
        .layer(middleware_concurrency)
        .layer(middleware::from_fn(process::postprocess_response))
        .layer(middleware::from_fn(process::preprocess_request))
        .layer(load_shedder_latency_mw);
```
