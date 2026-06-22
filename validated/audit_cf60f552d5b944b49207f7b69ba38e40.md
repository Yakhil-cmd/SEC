The code path is fully traceable. Let me confirm the `DEFAULT_CHUNK_SIZE` constant and the comment in `request_handler.rs` that acknowledges the lack of backpressure.

### Title
Unbounded Per-Request `encode_manifest` in `state_sync_chunk_handler` Enables CPU/Memory Exhaustion by a Byzantine Peer — (`rs/p2p/state_sync_manager/src/routes/chunk.rs`)

---

### Summary

A valid subnet peer can send up to 1,000 concurrent `StateSyncChunkRequest` messages targeting manifest chunk IDs (`MANIFEST_CHUNK_ID_OFFSET + N`). Each request causes the serving replica to re-encode the entire manifest from scratch inside a `spawn_blocking` task, then zstd-compress the result, with no application-level rate limit or concurrency cap per peer. For a large checkpoint (many canisters), the encoded manifest can exceed 100 MiB; 512+ concurrent blocking tasks each allocating that buffer can exhaust available RAM and saturate CPU on the serving replica.

---

### Finding Description

**Entry point — `state_sync_chunk_handler`**

Every incoming chunk request is dispatched unconditionally into `tokio::task::spawn_blocking`:

```rust
// rs/p2p/state_sync_manager/src/routes/chunk.rs:41-74
pub(crate) async fn state_sync_chunk_handler<T: 'static>(
    State(state): State<Arc<StateSyncChunkHandler<T>>>,
    payload: Bytes,
) -> Result<Bytes, StatusCode> {
    let jh = tokio::task::spawn_blocking(
        move || match state.state_sync.chunk(&artifact_id, chunk_id) {
            Some(data) => {
                ...
                let compressed = zstd::bulk::compress(&raw, zstd::DEFAULT_COMPRESSION_LEVEL)...;
                Ok(compressed)
            }
            None => Err(StatusCode::NO_CONTENT),
        },
    );
    ...
}
```

There is no semaphore, no per-peer counter, no `TOO_MANY_REQUESTS` guard, and no backpressure mechanism at the application layer. [1](#0-0) 

**Full manifest re-encoding on every manifest chunk request**

`StateSync::chunk()` calls `msg.get_chunk(chunk_id)`. Inside `StateSyncMessage::get_chunk`, the `ManifestChunk` arm unconditionally calls `encode_manifest(&self.manifest)` — serializing the entire manifest — then slices out the requested sub-manifest piece. There is no caching of the encoded bytes between calls:

```rust
// rs/state_manager/src/state_sync/types.rs:498-515
StateSyncChunk::ManifestChunk(index) => {
    let index = index as usize;
    if index < self.meta_manifest.sub_manifest_hashes.len() {
        let encoded_manifest = encode_manifest(&self.manifest);  // full re-encode every time
        let start = index * DEFAULT_CHUNK_SIZE as usize;
        ...
        payload = sub_manifest.to_vec();
    } else {
        return None;
    }
}
``` [2](#0-1) 

**QUIC stream limit is the only guard — and it is 1,000**

The QUIC transport sets `MAX_CONCURRENT_BIDI_STREAMS = VarInt::from_u32(1_000)` per connection. The request handler spawns a new Tokio task for every accepted stream with no further throttling. The code itself acknowledges this gap:

> "The extreme result of a slow handler is that the stream limit will be reached, hence having buffered up to the stream limit number of messages/requests. A better approach will be to use a router implemented as a tower service and accept streams iff the router is ready." [3](#0-2) [4](#0-3) 

**Manifest size at scale**

The test suite explicitly asserts that for a large state the encoded manifest exceeds 100 MiB:

```rust
// rs/state_manager/src/manifest/tests/compatibility.rs:593-595
assert!(
    encode_manifest(&manifest_v2).len() > 100 * DEFAULT_CHUNK_SIZE as usize,
    "The encoded manifest is supposed to be larger than 100 MiB."
);
``` [5](#0-4) 

`DEFAULT_CHUNK_SIZE` is 1 MiB, so the assertion confirms >100 MiB encoded manifests are a tested, expected scenario. [6](#0-5) 

---

### Impact Explanation

With 1,000 concurrent streams (the QUIC per-connection limit), the Tokio blocking thread pool (default cap: 512 threads) will run up to 512 simultaneous `encode_manifest` calls. Each allocates ≥100 MiB. Peak heap pressure: **≥50 GiB** of live allocations, plus the zstd compression buffers on top. This can trigger OOM on the serving replica or cause sustained CPU saturation, degrading or killing that replica's ability to participate in consensus and serve other traffic.

---

### Likelihood Explanation

The attacker must be a valid subnet peer authenticated via mutual TLS — i.e., a Byzantine node already admitted to the subnet. This is a realistic threat within the IC fault model (up to `f` Byzantine nodes tolerated). The attack requires no special knowledge beyond the advertised state height/hash (broadcast every 5 seconds by the victim itself). The chunk ID `MANIFEST_CHUNK_ID_OFFSET + 0` is always valid as long as the replica has a checkpoint, which is the normal operating condition.

---

### Recommendation

1. **Cache the encoded manifest** inside `StateSyncMessage` (e.g., a `once_cell::sync::OnceCell<Vec<u8>>`) so `encode_manifest` is called at most once per state, not once per chunk request.
2. **Add a per-peer concurrency semaphore** in `StateSyncChunkHandler` (e.g., `Arc<Semaphore>` keyed by `NodeId`) that limits concurrent `spawn_blocking` tasks per peer to a small value (e.g., 4–8).
3. **Return `TOO_MANY_REQUESTS`** when the semaphore cannot be acquired, which the client already handles gracefully (`DownloadChunkError::Overloaded`). [7](#0-6) 

---

### Proof of Concept

```
1. Replica A has a checkpoint with 5,000 canisters → encoded manifest > 100 MiB.
2. Byzantine peer B opens a QUIC connection to A (authenticated via subnet TLS).
3. B sends 1,000 concurrent StateSyncChunkRequest { id: <A's state id>, chunk_id: MANIFEST_CHUNK_ID_OFFSET + 0 }.
4. A's state_sync_chunk_handler spawns 1,000 spawn_blocking tasks.
5. Up to 512 run simultaneously; each calls encode_manifest() → ~100 MiB alloc → zstd compress.
6. Peak heap: ~50 GiB → OOM kill or severe memory pressure on A.
7. Repeat indefinitely; A cannot rate-limit B at the application layer.
```

### Citations

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

**File:** rs/state_manager/src/manifest/tests/compatibility.rs (L591-595)
```rust
    let manifest_v2 = Manifest::new(StateSyncVersion::V2, file_table, chunk_table);
    assert!(
        encode_manifest(&manifest_v2).len() > 100 * DEFAULT_CHUNK_SIZE as usize,
        "The encoded manifest is supposed to be larger than 100 MiB."
    );
```
