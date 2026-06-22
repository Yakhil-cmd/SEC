Based on my code analysis, here is my determination:

---

### Title
Unbounded Manifest Re-encoding Per Chunk Request Enables CPU/Memory Exhaustion on Serving Replica — (`rs/p2p/state_sync_manager/src/routes/chunk.rs`, `rs/state_manager/src/state_sync/types.rs`)

---

### Summary

A valid Byzantine subnet peer can flood the `/state-sync/chunk` endpoint with repeated `ManifestChunk` requests for a large-state replica. Each request triggers a full `encode_manifest()` call (allocating the entire encoded manifest in memory), followed by `zstd::bulk::compress()`, inside an unbounded `spawn_blocking` task. There is no per-peer rate limit, no semaphore, and no caching of the encoded manifest on the serving side.

---

### Finding Description

**Entrypoint**: `state_sync_chunk_handler` in `rs/p2p/state_sync_manager/src/routes/chunk.rs`.

Any valid subnet peer can send a `StateSyncChunkRequest` with `chunk_id >= MANIFEST_CHUNK_ID_OFFSET` (i.e., `>= 1 << 31`). The handler immediately spawns a blocking task with no concurrency guard:

```rust
let jh = tokio::task::spawn_blocking(
    move || match state.state_sync.chunk(&artifact_id, chunk_id) { ... }
);
``` [1](#0-0) 

This calls `StateSync::chunk()` → `StateSyncMessage::get_chunk()`. For a `ManifestChunk(index)` where `index < sub_manifest_hashes.len()`, the code unconditionally calls `encode_manifest(&self.manifest)` — allocating and serializing the **entire manifest** on every request — then slices out the relevant sub-manifest piece:

```rust
StateSyncChunk::ManifestChunk(index) => {
    let index = index as usize;
    if index < self.meta_manifest.sub_manifest_hashes.len() {
        let encoded_manifest = encode_manifest(&self.manifest); // full alloc every call
        ...
        payload = sub_manifest.to_vec();
    }
}
``` [2](#0-1) 

The encoded manifest is **not cached**. For a 5000-canister state, the encoded manifest exceeds 1 MiB (confirmed by the test at `rs/state_manager/tests/state_manager.rs:3582-3605`), producing at least 2 valid sub-manifest chunk IDs (`MANIFEST_CHUNK_ID_OFFSET + 0`, `MANIFEST_CHUNK_ID_OFFSET + 1`). [3](#0-2) 

`MANIFEST_CHUNK_ID_OFFSET = 1 << 31`: [4](#0-3) 

**Transport-level concurrency**: The QUIC transport sets `MAX_CONCURRENT_BIDI_STREAMS = 1000` per connection. The `start_stream_acceptor` loop spawns a new task for every accepted stream with **no inflight cap**: [5](#0-4) [6](#0-5) 

The comment in the transport code explicitly acknowledges this gap: *"The extreme result of a slow handler is that the stream limit will be reached, hence having buffered up to the stream limit number of messages/requests."*

**No rate limiting in the chunk handler**: `StateSyncChunkHandler` holds no semaphore, no per-peer counter, and no request queue: [7](#0-6) 

---

### Impact Explanation

With 1000 concurrent streams (the QUIC per-connection limit), a Byzantine peer can cause up to 1000 simultaneous `encode_manifest()` calls. For a 5 MiB encoded manifest, this is ~5 GiB of concurrent heap allocations plus zstd compression CPU on the serving replica's blocking thread pool. This can cause:

- Severe memory pressure / OOM on the serving replica
- Blocking thread pool saturation (Tokio default: 512 threads), starving other legitimate blocking operations
- Single-replica availability degradation (the targeted replica may fall behind in consensus participation)

---

### Likelihood Explanation

The attacker only needs to be a valid subnet peer (below the Byzantine fault threshold). The valid manifest chunk IDs are predictable (`MANIFEST_CHUNK_ID_OFFSET + 0`, `+1`, etc.). No special knowledge beyond subnet membership is required. The attack is trivially repeatable and requires no coordination.

---

### Recommendation

1. **Cache the encoded manifest** in `StateSyncMessage` so `encode_manifest()` is called at most once per state, not once per chunk request.
2. **Add a per-peer concurrency semaphore** in `StateSyncChunkHandler` to cap simultaneous `spawn_blocking` tasks per peer (similar to the semaphore pattern used in `rs/http_endpoints/xnet/src/lib.rs`). [8](#0-7) 
3. Consider returning `StatusCode::TOO_MANY_REQUESTS` when the semaphore is exhausted (the client-side parser already handles this status code). [9](#0-8) 

---

### Proof of Concept

1. Spin up a replica with a 5000-canister checkpoint (encoded manifest > 1 MiB, ≥ 2 valid sub-manifest chunks).
2. From a valid subnet peer, open 1000 concurrent QUIC streams to the serving replica's transport port.
3. On each stream, send a `StateSyncChunkRequest` with `chunk_id = MANIFEST_CHUNK_ID_OFFSET` (= `2_147_483_648`) and the correct `StateSyncArtifactId`.
4. Observe: the serving replica's memory usage spikes by several GiB; blocking thread pool saturates; replica latency increases significantly.

The call chain is: `state_sync_chunk_handler` → `spawn_blocking` → `StateSync::chunk` → `StateSyncMessage::get_chunk` → `encode_manifest` (full manifest allocation) → `zstd::bulk::compress`. [10](#0-9) [11](#0-10) [2](#0-1)

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

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L41-74)
```rust
pub(crate) async fn state_sync_chunk_handler<T: 'static>(
    State(state): State<Arc<StateSyncChunkHandler<T>>>,
    payload: Bytes,
) -> Result<Bytes, StatusCode> {
    // Parse payload
    let pb::StateSyncChunkRequest { id, chunk_id } =
        pb::StateSyncChunkRequest::decode(payload).map_err(|_| StatusCode::BAD_REQUEST)?;
    let artifact_id: StateSyncArtifactId = id.map(From::from).ok_or(StatusCode::BAD_REQUEST)?;
    let chunk_id = ChunkId::from(chunk_id);

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
    let data = jh.await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)??;

    Ok(data.into())
}
```

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L129-129)
```rust
        StatusCode::TOO_MANY_REQUESTS => Err(DownloadChunkError::Overloaded),
```

**File:** rs/state_manager/src/state_sync/types.rs (L123-123)
```rust
pub const MANIFEST_CHUNK_ID_OFFSET: u32 = 1 << 31;
```

**File:** rs/state_manager/src/state_sync/types.rs (L498-515)
```rust
                StateSyncChunk::ManifestChunk(index) => {
                    let index = index as usize;
                    if index < self.meta_manifest.sub_manifest_hashes.len() {
                        let encoded_manifest = encode_manifest(&self.manifest);
                        let start = index * DEFAULT_CHUNK_SIZE as usize;
                        let end = std::cmp::min(
                            start + DEFAULT_CHUNK_SIZE as usize,
                            encoded_manifest.len(),
                        );
                        let sub_manifest = encoded_manifest.get(start..end).unwrap_or_else(||
                            panic!("We cannot get the {index}th piece of the encoded manifest. The manifest and/or meta-manifest must be in abnormal state.")
                        );
                        payload = sub_manifest.to_vec();
                    } else {
                        // The chunk request is either malicious or invalid due to the collision between normal file chunks and manifest chunks.
                        // Neither case could be resolved and a `None` has to be returned in both cases.
                        return None;
                    }
```

**File:** rs/state_manager/tests/state_manager.rs (L3582-3605)
```rust
        // Insert large number of canisters so that the encoded manifest is larger than 1 MiB.
        let num_canisters = 5000;
        for id in 100..(100 + num_canisters) {
            insert_dummy_canister(&mut state, canister_test_id(id));
        }

        src_state_manager.commit_and_certify(state, CertificationScope::Full, None);
        let hash = wait_for_checkpoint(&*src_state_manager, Height(1));
        let id = StateSyncArtifactId {
            height: Height(1),
            hash: hash.get_ref().clone(),
        };

        let state = src_state_manager.get_latest_state().take();

        let msg = src_state_sync
            .get(&id)
            .expect("failed to get state sync messages");

        let meta_manifest = build_meta_manifest(&msg.manifest);
        assert!(
            meta_manifest.sub_manifest_hashes.len() >= 2,
            "The test should run with the manifest chunked in multiple pieces."
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

**File:** rs/state_manager/src/state_sync.rs (L427-430)
```rust
    fn chunk(&self, id: &StateSyncArtifactId, chunk_id: ChunkId) -> Option<Chunk> {
        let msg = self.get(id)?;
        msg.get_chunk(chunk_id)
    }
```
