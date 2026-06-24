Looking at the actual code to verify each claim in the exploit path.

Based on my code review, here is my determination:

---

### Title
Unbounded `spawn_blocking` in `state_sync_chunk_handler` Allows Single Byzantine Peer to Exhaust Tokio Blocking Thread Pool — (`rs/p2p/state_sync_manager/src/routes/chunk.rs`)

### Summary
A TLS-authenticated subnet peer can send up to 1,000 concurrent `POST /state-sync/chunk` requests (bounded only by `MAX_CONCURRENT_BIDI_STREAMS`), each of which unconditionally calls `tokio::task::spawn_blocking` with blocking file I/O inside `StateSync::chunk()`. There is no semaphore, per-peer rate limit, or concurrency cap anywhere in the state sync chunk serving path. This can saturate Tokio's shared blocking thread pool, starving other subsystems that also rely on `spawn_blocking`.

### Finding Description

**Missing guard — no semaphore or concurrency cap:**

The handler unconditionally issues a `spawn_blocking` for every incoming request: [1](#0-0) 

There is no `Semaphore::try_acquire` or equivalent. Compare this to the XNet endpoint, which explicitly guards its `spawn_blocking` with a semaphore and returns `503` when the pool is full: [2](#0-1) 

**QUIC stream limit allows 1,000 concurrent streams per peer:** [3](#0-2) 

`MAX_CONCURRENT_BIDI_STREAMS = 1000` per connection. A single peer can open 1,000 concurrent bidirectional streams, each accepted without backpressure: [4](#0-3) 

The comment at lines 52–56 explicitly acknowledges this design gap: *"The extreme result of a slow handler is that the stream limit will be reached, hence having buffered up to the stream limit number of messages/requests."*

**No per-peer concurrency limit in state sync manager:** [5](#0-4) 

`StateSyncChunkHandler` holds no semaphore, counter, or rate-limiting state.

**Other subsystems share the same blocking pool:**

Query validation also uses `spawn_blocking` on the same runtime: [6](#0-5) 

### Impact Explanation
A single Byzantine subnet peer sends 1,000 concurrent chunk requests. Each triggers a `spawn_blocking` call that blocks on disk I/O reading a ~1 MB state chunk. Tokio's default blocking pool (512 threads) saturates. Other `spawn_blocking` callers — query validation, XNet pool refill — queue indefinitely. The target replica stalls on query handling and state sync processing. This is a **non-volumetric single-replica availability degradation** requiring only one authenticated peer.

### Likelihood Explanation
The attacker precondition (valid subnet peer) is achievable by any Byzantine node below the consensus fault threshold. The exploit requires only opening concurrent QUIC streams — no special protocol knowledge beyond what a normal state sync client does. The missing guard is a straightforward code omission, not a subtle race.

### Recommendation
Add a bounded `tokio::sync::Semaphore` to `StateSyncChunkHandler`, mirroring the XNet endpoint pattern. On `try_acquire` failure, return `StatusCode::TOO_MANY_REQUESTS` (the client-side parser already handles this code at line 129 of `chunk.rs`): [7](#0-6) 

Additionally, consider reducing `MAX_CONCURRENT_BIDI_STREAMS` or enforcing a per-peer application-level concurrency cap in the stream acceptor.

### Proof of Concept
1. Implement a mock `StateSyncClient::chunk()` that sleeps for 500 ms (simulating disk I/O).
2. From one authenticated peer, open 600 concurrent QUIC streams each sending a valid `StateSyncChunkRequest`.
3. Concurrently, from a second subsystem, call `tokio::task::spawn_blocking(|| std::thread::sleep(Duration::from_millis(1)))`.
4. Assert the second `spawn_blocking` does not complete within 5 seconds — demonstrating pool starvation.

The `MAX_CONCURRENT_BIDI_STREAMS = 1000` ceiling and the absence of any semaphore in `state_sync_chunk_handler` make this directly reproducible in a unit test without any network infrastructure.

### Citations

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L21-39)
```rust
pub(crate) struct StateSyncChunkHandler<T> {
    _log: ReplicaLogger,
    state_sync: Arc<dyn StateSyncClient<Message = T>>,
    metrics: StateSyncManagerHandlerMetrics,
}

impl<T> StateSyncChunkHandler<T> {
    pub fn new(
        log: ReplicaLogger,
        state_sync: Arc<dyn StateSyncClient<Message = T>>,
        metrics: StateSyncManagerHandlerMetrics,
    ) -> Self {
        Self {
            _log: log,
            state_sync,
            metrics,
        }
    }
}
```

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L51-70)
```rust
    let jh =
        tokio::task::spawn_blocking(
            move || match state.state_sync.chunk(&artifact_id, chunk_id) {
                Some(data) => {
                    let pb_chunk = pb::StateSyncChunkResponse { data: data.take() };
                    let mut raw = BytesMut::with_capacity(pb_chunk.encoded_len());
                    pb_chunk.encode(&mut raw).expect("Allocated enough memory");
                    let raw = raw.freeze();

                    let compressed = zstd::bulk::compress(&raw, zstd::DEFAULT_COMPRESSION_LEVEL)
                        .expect("Compression failed");
                    state
                        .metrics
                        .compression_ratio
                        .observe(raw.len() as f64 / compressed.len() as f64);
                    Ok(compressed)
                }
                None => Err(StatusCode::NO_CONTENT),
            },
        );
```

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L129-129)
```rust
        StatusCode::TOO_MANY_REQUESTS => Err(DownloadChunkError::Overloaded),
```

**File:** rs/http_endpoints/xnet/src/lib.rs (L156-168)
```rust
    let owned_permit = match ctx.semaphore.try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => {
            ctx.metrics
                .request_duration
                .with_label_values(&[RESOURCE_UNKNOWN, StatusCode::SERVICE_UNAVAILABLE.as_str()])
                .observe(0.0);

            return ok(Response::builder()
                .status(StatusCode::SERVICE_UNAVAILABLE)
                .body(Body::from("Queue full"))
                .unwrap());
        }
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L74-75)
```rust
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L50-56)
```rust
    let mut inflight_requests: JoinSet<Result<(), P2PError>> = tokio::task::JoinSet::new();
    let mut quic_metrics_scrape = tokio::time::interval(QUIC_METRIC_SCRAPE_INTERVAL);
    // The extreme result of a slow handler is that the stream limit will be reach, hence
    // having buffered up to the stream limit number of messages/requests.
    // A better approach will be to use a router implemented as a tower service and accept
    // streams iff the router is ready. Then the actual number of buffered messages is determined
    // by the handlers instead by the underlying implementation.
```

**File:** rs/http_endpoints/public/src/query.rs (L277-284)
```rust
    match tokio::task::spawn_blocking(move || {
        validator.validate_request(
            &request_c,
            time_source.get_relative_time(),
            &root_of_trust_provider,
        )
    })
    .await
```
