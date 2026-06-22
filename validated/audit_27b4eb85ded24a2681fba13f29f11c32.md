The code is confirmed. Here is the analysis:

**Key facts from the code:**

1. `extract_canister_id` calls `to_bytes(body, usize::MAX)` with no upper bound: [1](#0-0) 

2. `RosettaMetricsMiddleware::call` invokes this for every non-`/metrics` request, before any routing: [2](#0-1) 

3. The ICRC-1 Rosetta Axum router applies `metrics_layer` to all routes with **no** `DefaultBodyLimit`, `RequestBodyLimitLayer`, or any body-size guard anywhere in the stack: [3](#0-2) 

4. By contrast, the ICP Rosetta (Actix-web) does enforce a 4 MB JSON body limit: [4](#0-3) 

---

### Title
Unbounded Body Buffering in `RosettaMetricsMiddleware` Allows Unauthenticated OOM Crash of ICRC-1 Rosetta Process — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

### Summary
The Axum-based ICRC-1 Rosetta server applies `RosettaMetricsMiddleware` to every HTTP endpoint. Inside that middleware, `extract_canister_id` calls `to_bytes(body, usize::MAX)`, buffering the entire request body into heap memory with no size limit. Because no upstream body-size guard exists in the router, any unauthenticated client can send an arbitrarily large POST body to any Rosetta endpoint and exhaust the process heap, causing an OOM kill.

### Finding Description
In `rs/rosetta-api/common/rosetta_core/src/metrics.rs`, `extract_canister_id` reads the full HTTP body into a `Bytes` buffer:

```rust
let bytes = to_bytes(body, usize::MAX).await?;
```

`usize::MAX` is the sentinel value meaning "no limit." This function is called unconditionally from `RosettaMetricsMiddleware::call` for every request that is not `GET /metrics`, before the request is dispatched to any route handler.

In `rs/rosetta-api/icrc1/src/main.rs`, the router is built as:

```rust
let app = Router::new()
    .route("/construction/submit", post(construction_submit))
    // ... all other routes ...
    .layer(metrics_layer)   // ← outermost layer, no body limit anywhere
    .layer(add_request_span())
    .layer(RequestIdLayer)
    .with_state(token_app_states.clone());
```

There is no `tower_http::limit::RequestBodyLimitLayer`, no `axum::extract::DefaultBodyLimit`, and no `Content-Length` rejection anywhere in the stack. The ICP Rosetta (Actix-web) has a 4 MB `JsonConfig` limit, but the ICRC-1 Rosetta (Axum) has none.

### Impact Explanation
An unauthenticated attacker sends a single HTTP POST (e.g., to `/construction/submit`) with a multi-gigabyte body. The middleware buffers the entire body before the route handler ever runs. The Rosetta process is a single-process, single-replica service; exhausting its heap causes an OOM kill, taking down the entire ICRC-1 Rosetta instance. Recovery requires an operator restart.

### Likelihood Explanation
The attack requires only a TCP connection and the ability to stream bytes — no credentials, no tokens, no prior knowledge beyond the open port. The Rosetta HTTP port is typically exposed to clients. A single request is sufficient to trigger the condition.

### Recommendation
Add an explicit body-size limit as the outermost layer in the Axum router, before `metrics_layer`:

```rust
use axum::extract::DefaultBodyLimit;

let app = Router::new()
    // ... routes ...
    .layer(metrics_layer)
    .layer(add_request_span())
    .layer(RequestIdLayer)
    .layer(DefaultBodyLimit::max(4 * 1024 * 1024)) // 4 MB, consistent with ICP Rosetta
    .with_state(token_app_states.clone());
```

Alternatively, replace `usize::MAX` in `extract_canister_id` with a reasonable constant (e.g., `4 * 1024 * 1024`) so the middleware itself is safe regardless of whether a router-level limit is present.

### Proof of Concept
```bash
# Stream a 2 GB body to any Rosetta endpoint; observe OOM kill before any 413 is returned
dd if=/dev/zero bs=1M count=2048 | \
  curl -X POST http://<rosetta-host>:8080/construction/submit \
       -H "Content-Type: application/json" \
       --data-binary @- \
       -v
```
Monitor the Rosetta process with `dmesg | grep -i oom` or `journalctl -u rosetta` to confirm the OOM kill before any response is sent.

### Citations

**File:** rs/rosetta-api/common/rosetta_core/src/metrics.rs (L331-339)
```rust
        Box::pin(async move {
            // Need to buffer the body to extract the canister ID
            let (parts, body) = req.into_parts();

            // Extract canister ID from body if possible
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

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L296-303)
```rust
                .app_data(web::Data::new(
                    web::JsonConfig::default()
                        .limit(4 * 1024 * 1024)
                        .error_handler(move |e, _| {
                            errors::convert_to_error(&ApiError::invalid_request(format!("{e:#?}")))
                                .into()
                        }),
                ))
```
