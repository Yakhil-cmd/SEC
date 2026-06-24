Based on my code review, here is the full assessment:

---

### Title
Unbounded `spawn_blocking` per Chunk Request Allows Single Byzantine Peer to Exhaust Tokio Blocking Thread Pool — (`rs/p2p/state_sync_manager/src/routes/chunk.rs`)

### Summary

`state_sync_chunk_handler` calls `tokio::task::spawn_blocking` unconditionally for every incoming chunk request with no per-peer concurrency cap. The QUIC transport layer permits up to **1,000 concurrent bidirectional streams per peer connection**, which exceeds Tokio's default blocking thread pool limit of 512. A single TLS-authenticated subnet peer can flood the target replica with concurrent `StateSyncChunkRequest`s, saturating the shared blocking thread pool and starving all other `spawn_blocking` callers (query validation, xnet pool refill, etc.).

### Finding Description

**Entry point — chunk handler:**

In `state_sync_chunk_handler`, every request unconditionally spawns a blocking task: [1](#0-0) 

There is no semaphore, rate limiter, or per-peer concurrency guard anywhere in this handler or in the `StateSyncChunkHandler` struct. [2](#0-1) 

**QUIC stream limit — 1,000 per peer:**

The transport layer sets `MAX_CONCURRENT_BIDI_STREAMS = VarInt::from_u32(1_000)`, meaning a single authenticated peer connection may open up to 1,000 concurrent streams: [3](#0-2) 

**Request handler — unbounded JoinSet:**

`start_stream_acceptor` accepts every incoming stream and spawns a tokio task for it with no backpressure or size cap on `inflight_requests`. The code itself acknowledges this gap: [4](#0-3) 

**Attack chain:**

1. Byzantine peer (valid subnet member, TLS-authenticated) opens 1,000 concurrent QUIC bidi streams to the target replica.
2. Each stream is accepted by `start_stream_acceptor` → `handle_bi_stream` → `state_sync_chunk_handler`.
3. Each handler call issues `tokio::task::spawn_blocking(state_sync.chunk(...))`, which performs synchronous file I/O.
4. With 1,000 concurrent blocking tasks and file I/O holding threads, Tokio's blocking pool (default 512 threads) saturates.
5. All subsequent `spawn_blocking` callers — including query validation (`rs/http_endpoints/public/src/query.rs`) and xnet pool refill (`rs/xnet/payload_builder/src/lib.rs`) — queue indefinitely. [5](#0-4) 

### Impact Explanation

- **State sync stall**: the target replica cannot serve or receive state sync chunks while the pool is saturated.
- **Query handling degradation**: query validation uses `spawn_blocking` and will queue behind the flood.
- **Xnet payload building**: also uses `spawn_blocking` for pool refill, causing cross-subnet message delays.
- Impact is scoped to a **single replica** (non-volumetric, single-replica availability degradation), consistent with the stated scope.

### Likelihood Explanation

- Attacker only needs to be a valid subnet peer — no key compromise, no majority corruption required.
- The QUIC stream limit (1,000) is a hard protocol-level cap that already exceeds the Tokio blocking pool default (512).
- File I/O duration inside `StateSync::chunk()` is the only variable; even moderate latency (tens of milliseconds per chunk read) is sufficient to keep the pool saturated under 1,000 concurrent requests.
- The design gap is explicitly acknowledged in the codebase comment at `request_handler.rs:52–56`.

### Recommendation

1. **Add a per-peer concurrency semaphore** in `state_sync_chunk_handler` (e.g., `Arc<Semaphore>` with a limit well below 512, stored in `StateSyncChunkHandler`). Return `429 Too Many Requests` when the semaphore cannot be acquired.
2. **Alternatively**, use a dedicated bounded `ThreadPool` (like the crypto vault does) for state sync chunk serving, isolating it from the shared Tokio blocking pool.
3. **Apply backpressure at the stream acceptor level** by making the router a Tower service and only accepting new streams when the router is ready, as the existing TODO comment in `request_handler.rs` suggests.

### Proof of Concept

```rust
// Mock StateSyncClient::chunk() that sleeps 100ms (simulating file I/O)
// Send 600 concurrent POST /state-sync/chunk requests from one peer
// Assert that a concurrent spawn_blocking from another subsystem
// does not complete within 30s (pool fully saturated)

let state_sync = Arc::new(SlowMockStateSync { delay: Duration::from_millis(100) });
let handler = StateSyncChunkHandler::new(log, state_sync, metrics);
// Spawn 600 concurrent requests — exceeds Tokio default 512 blocking threads
let handles: Vec<_> = (0..600).map(|_| {
    tokio::spawn(state_sync_chunk_handler(State(handler.clone()), valid_payload()))
}).collect();
// Concurrently attempt a spawn_blocking from another subsystem
let other = tokio::task::spawn_blocking(|| "other subsystem work");
// This will queue indefinitely while the 512 threads are all blocked
assert!(tokio::time::timeout(Duration::from_secs(30), other).await.is_err());
```

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
