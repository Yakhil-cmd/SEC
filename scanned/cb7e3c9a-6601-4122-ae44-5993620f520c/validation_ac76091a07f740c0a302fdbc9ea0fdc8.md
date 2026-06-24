### Title
Unbounded Heap Allocation via `to_bytes(body, usize::MAX)` in ICRC1 Rosetta Metrics Middleware — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

---

### Summary

The `extract_canister_id` function in the `RosettaMetricsMiddleware` calls `to_bytes(body, usize::MAX)` to buffer the entire HTTP request body into heap memory before any size validation occurs. The ICRC1 Rosetta server applies this middleware with no upstream `RequestBodyLimitLayer` or `DefaultBodyLimit` anywhere in its router stack. An unprivileged HTTP client can send an arbitrarily large POST body to any Rosetta endpoint, causing unbounded heap allocation and potential OOM crash or severe memory pressure in the Rosetta process.

---

### Finding Description

In `extract_canister_id`, the body is consumed unconditionally with no size cap:

```rust
let bytes = to_bytes(body, usize::MAX).await?;
``` [1](#0-0) 

This is called for **every** incoming request inside `RosettaMetricsMiddleware::call`, before the request is forwarded to any handler: [2](#0-1) 

The ICRC1 Rosetta server builds its axum router and applies `metrics_layer` as the innermost middleware, with no body size limit layer anywhere in the chain: [3](#0-2) 

The server starts with a bare `axum::serve` call — no `DefaultBodyLimit::disable()` + `RequestBodyLimitLayer`, no `DefaultBodyLimit::max(N)`: [4](#0-3) 

A grep across the entire `rs/rosetta-api/` tree for `RequestBodyLimitLayer`, `DefaultBodyLimit`, `max_request_size`, or any body-limit pattern returns **zero matches**, confirming no limit exists anywhere in the Rosetta API codebase.

For contrast, the IC public HTTP endpoint correctly applies a `RequestBodyLimitLayer` globally before any middleware reads the body: [5](#0-4) 

The ICP Rosetta server (actix-web) does apply a 4 MB JSON limit, but that is a separate binary and does not protect the ICRC1 Rosetta axum server: [6](#0-5) 

---

### Impact Explanation

Any unauthenticated HTTP client that can reach the ICRC1 Rosetta port can POST an arbitrarily large body (e.g., 1 GB) to any endpoint (e.g., `/network/list`, `/block`, `/construction/submit`). The metrics middleware will allocate a contiguous `Bytes` buffer of that size on the heap before the handler ever runs. A single such request can exhaust available memory, crashing the Rosetta process (OOM kill) or causing severe memory pressure that degrades availability for all legitimate users.

---

### Likelihood Explanation

The Rosetta HTTP port is intended to be publicly reachable (it is the Rosetta API). No authentication is required to POST to any endpoint. The exploit requires only a single HTTP connection with a large streaming body and is trivially reproducible with `curl --data-binary`. The only mitigation would be an external reverse proxy with a body-size limit, which is a deployment-level control not enforced by the code.

---

### Recommendation

Apply a body size limit **before** the metrics middleware in the ICRC1 Rosetta router. In axum this is done by adding `DefaultBodyLimit::max(N)` or `RequestBodyLimitLayer::new(N)` as the outermost layer (last `.layer()` call), or by replacing `usize::MAX` in `to_bytes` with a reasonable constant (e.g., 4 MB):

```rust
// Option A: limit at the router level (outermost layer)
.layer(DefaultBodyLimit::max(4 * 1024 * 1024))

// Option B: limit inside extract_canister_id
const MAX_BODY_BYTES: usize = 4 * 1024 * 1024;
let bytes = to_bytes(body, MAX_BODY_BYTES).await?;
```

Option A is preferred because it prevents the body from being read at all beyond the limit, returning 413 before any allocation occurs.

---

### Proof of Concept

```bash
# Send a 512 MB body to the ICRC1 Rosetta /network/list endpoint
dd if=/dev/zero bs=1M count=512 | \
  curl -X POST http://<rosetta-host>:<port>/network/list \
       -H "Content-Type: application/json" \
       --data-binary @- \
       -v
```

The process will allocate ~512 MB on the heap inside `to_bytes` before returning any response. Repeating with concurrent connections or increasing body size to available RAM will OOM-kill the Rosetta process.

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

**File:** rs/rosetta-api/icrc1/src/main.rs (L474-476)
```rust
    axum::serve(tcp_listener, app.into_make_service())
        .await
        .context("Unable to start the Rosetta server")
```

**File:** rs/http_endpoints/public/src/lib.rs (L687-691)
```rust
            // Disable default limit since apply a request limit to all routes.
            .layer(DefaultBodyLimit::disable())
            .layer(RequestBodyLimitLayer::new(
                config.max_request_size_bytes as usize,
            ))
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
