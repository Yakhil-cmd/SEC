The code is confirmed. Here is the analysis:

**Key facts from the production code:**

1. `extract_canister_id` unconditionally calls `to_bytes(body, usize::MAX).await?` with no size bound: [1](#0-0) 

2. `RosettaMetricsMiddleware::call` invokes `extract_canister_id` for every non-`/metrics` request, before any handler logic runs: [2](#0-1) 

3. The ICRC1 `main.rs` Axum stack applies layers in this order (outermost → innermost): `RequestIdLayer` → `add_request_span()` → `metrics_layer` → handlers. None of the outer layers impose a body size limit: [3](#0-2) 

4. The server binds to `0.0.0.0`, making it network-accessible to any unauthenticated client: [4](#0-3) 

5. The sync threads and HTTP server share the same OS process; an OOM kill terminates both: [5](#0-4) 

---

### Title
Unbounded Body Buffering in `RosettaMetricsMiddleware` Enables Unauthenticated OOM DoS — (`rs/rosetta-api/common/rosetta_core/src/metrics.rs`)

### Summary
`RosettaMetricsMiddleware::call` buffers the entire HTTP request body into heap memory via `to_bytes(body, usize::MAX)` before any size validation or routing occurs. Any unauthenticated client can stream an arbitrarily large body to any Rosetta POST endpoint, exhausting process heap and triggering an OOM kill that terminates both the HTTP server and all ledger sync threads.

### Finding Description
In `extract_canister_id` (line 389 of `metrics.rs`), the call `to_bytes(body, usize::MAX).await?` passes `usize::MAX` as the size limit, which is the axum/hyper sentinel meaning "no limit — buffer everything." This function is called unconditionally for every non-`/metrics` request inside `RosettaMetricsMiddleware::call` (line 336). The purpose is to peek at the JSON body to extract a `network_identifier.network` field for metrics labeling — a purely observability concern — but it does so by consuming the entire body stream into a contiguous heap allocation.

The Axum middleware stack in `main.rs` (lines 382–390) applies `metrics_layer` as the innermost middleware, with only `RequestIdLayer` and a tracing span layer outside it. Neither applies any body size limit. There is no `tower_http::limit::RequestBodyLimitLayer` or equivalent anywhere in the stack.

### Impact Explanation
A single HTTP connection sending a chunked-transfer-encoded POST body of arbitrary size (no `Content-Length` required) to any endpoint (e.g., `/block`, `/account/balance`, `/construction/submit`) will cause the Tokio runtime to allocate heap memory proportional to the body size. At sufficient scale (e.g., a few GB), the Linux OOM killer terminates the process. This kills:
- The HTTP server (all Rosetta API requests fail)
- All `tokio::spawn`-ed ledger sync threads (block synchronization halts)
- The watchdog threads (no automatic recovery until the process is restarted externally)

If Rosetta is the sole withdrawal processor for ckBTC/ckETH, all in-flight withdrawals stall until the process is restarted and re-synced.

### Likelihood Explanation
The attack requires only a TCP connection to the Rosetta port and the ability to send a streaming HTTP POST. No authentication, no credentials, no privileged access. The Rosetta server binds to `0.0.0.0` and is intended to be reachable by exchange integrators. A single attacker with a fast uplink can sustain the DoS indefinitely by reconnecting after each OOM kill.

### Recommendation
Replace `usize::MAX` with a reasonable bound (e.g., 10 MB) in `extract_canister_id`:

```rust
// Before
let bytes = to_bytes(body, usize::MAX).await?;

// After
const MAX_BODY_BYTES: usize = 10 * 1024 * 1024; // 10 MB
let bytes = to_bytes(body, MAX_BODY_BYTES).await?;
```

Additionally, add `tower_http::limit::RequestBodyLimitLayer` as the outermost middleware in `main.rs` to enforce the limit at the HTTP layer before any middleware processes the body.

### Proof of Concept
```python
import socket, time

HOST, PORT = "rosetta.example.com", 8080
s = socket.create_connection((HOST, PORT))
# Chunked POST with no Content-Length
s.sendall(b"POST /block HTTP/1.1\r\nHost: rosetta\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n")
chunk = b"a" * 65536
header = f"{len(chunk):x}\r\n".encode()
while True:
    s.sendall(header + chunk + b"\r\n")
    time.sleep(0.001)  # sustain stream
```
Running this against an unprotected Rosetta instance will cause RSS to grow unboundedly until the process is OOM-killed. Assert: process RSS grows linearly with bytes sent; no HTTP 413 is returned before OOM.

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

**File:** rs/rosetta-api/icrc1/src/main.rs (L393-394)
```rust
    let rosetta_url = format!("0.0.0.0:{}", get_port(config.port, &config.port_file));
    let tcp_listener = TcpListener::bind(rosetta_url.clone()).await?;
```

**File:** rs/rosetta-api/icrc1/src/main.rs (L416-468)
```rust
            tokio::spawn(
                async move {
                    // First heartbeat might take hours until the ledger is initially synced,
                    // so we skip it to avoid the watchdog thread to restart the sync thread
                    // during the initial synchronization.
                    let skip_first_hearbeat = true;
                    let local_state = Arc::clone(&shared_state);
                    let mut watchdog = WatchdogThread::new(
                        Duration::from_secs(config.watchdog_timeout_seconds),
                        Some(Arc::new(move || {
                            local_state.storage.get_metrics().inc_sync_thread_restarts();
                            info!("Watchdog triggered restart for a sync thread");
                        })),
                        skip_first_hearbeat,
                        Some(span_watchdog.clone()),
                    );
                    let span_watchdog = span_watchdog.clone();
                    watchdog.start(move |heartbeat| {
                        let shared_state = Arc::clone(&shared_state);
                        let span_watchdog = span_watchdog.clone();
                        tokio::spawn(
                            async move {
                                if let Err(e) = start_synching_blocks(
                                    shared_state.icrc1_agent.clone(),
                                    shared_state.storage.clone(),
                                    MAXIMUM_BLOCKS_PER_REQUEST,
                                    shared_state.archive_canister_ids.clone(),
                                    RecurrencyMode::Recurrent(RecurrencyConfig {
                                        min_recurrency_wait: Duration::from_secs(
                                            BLOCK_SYNC_WAIT_SECS,
                                        ),
                                        max_recurrency_wait: Duration::from_secs(
                                            MAX_BLOCK_SYNC_WAIT_SECS,
                                        ),
                                        backoff_factor: 2,
                                    }),
                                    Box::new(heartbeat),
                                )
                                .await
                                {
                                    error!(
                                        "Sync error for token {:?}: {:?}",
                                        shared_state.ledger_id, e
                                    );
                                }
                            }
                            .instrument(span_watchdog),
                        )
                    });
                }
                .instrument(span),
            );
        }
```
