Now I have all the information needed to make a determination. Let me verify the chunk handler and the `chunk()` method implementation in state_sync.rs.

### Title
Repeated `ManifestChunk` Requests Trigger Uncached `encode_manifest` Per Call, Enabling CPU Exhaustion — (`rs/state_manager/src/state_sync/types.rs`)

---

### Summary

Every `ManifestChunk` request served by `StateSyncMessage::get_chunk` unconditionally re-serializes the entire manifest via `encode_manifest`. There is no cache, and the P2P chunk handler has no rate limiting. A Byzantine subnet peer can flood the serving replica with repeated manifest-chunk requests, causing O(manifest\_size) CPU work per request and sustained CPU exhaustion.

---

### Finding Description

In `StateSyncMessage::get_chunk`, the `ManifestChunk` branch calls `encode_manifest(&self.manifest)` on every invocation: [1](#0-0) 

`encode_manifest` performs a full protobuf serialization of the manifest: [2](#0-1) 

The result is not stored anywhere on `StateSyncMessage`; the struct holds only the raw `Manifest`: [3](#0-2) 

The P2P chunk handler in `rs/p2p/state_sync_manager/src/routes/chunk.rs` dispatches every incoming request directly to `state_sync.chunk()` via `spawn_blocking` with **no per-peer rate limiting, no deduplication, and no concurrency cap**: [4](#0-3) 

A search for `rate_limit`, `TOO_MANY_REQUESTS`, and `throttle` in the entire `rs/p2p/state_sync_manager/src/` tree returns zero matches, confirming the absence of any admission control on the serving side.

---

### Impact Explanation

For a large subnet checkpoint (e.g., 100 MiB encoded manifest), each `ManifestChunk` request forces a full protobuf serialization of the manifest. A Byzantine peer sending requests in a tight loop causes:

1. Sustained high CPU usage on the serving replica's blocking thread pool (Tokio `spawn_blocking`).
2. Potential exhaustion of the blocking thread pool, stalling other blocking I/O operations (checkpoint reads, file chunk serving).
3. Degraded consensus participation and legitimate state-sync serving on the victim replica.

The number of valid manifest chunk IDs is `meta_manifest.sub_manifest_hashes.len()` (one per 1 MiB of encoded manifest), so for a 100 MiB manifest there are ~100 valid IDs, all of which trigger the full re-serialization.

---

### Likelihood Explanation

The attacker must be a valid subnet peer (a Byzantine node below the consensus fault threshold). This is explicitly within the stated attack surface ("protocol peer behavior below the consensus fault threshold"). No admin key, governance majority, or threshold corruption is required — a single Byzantine node suffices. The exploit is mechanically trivial: send repeated HTTP/QUIC requests to the `/state-sync/chunk` endpoint with `chunk_id = MANIFEST_CHUNK_ID_OFFSET`.

---

### Recommendation

Cache the encoded manifest bytes inside `StateSyncMessage` (e.g., as a `once_cell::sync::OnceCell<Vec<u8>>` or pre-computed `Arc<Vec<u8>>`), so `encode_manifest` is called at most once per checkpoint regardless of how many `ManifestChunk` requests arrive. Additionally, add per-peer request rate limiting in `state_sync_chunk_handler`.

---

### Proof of Concept

```rust
// Pseudocode benchmark
let msg: StateSyncMessage = /* large checkpoint with ~100 MiB manifest */;
let chunk_id = ChunkId::new(MANIFEST_CHUNK_ID_OFFSET); // valid ManifestChunk(0)

let start = Instant::now();
for _ in 0..1000 {
    let _ = msg.get_chunk(chunk_id); // each call invokes encode_manifest()
}
let total = start.elapsed();
// Expected: total ≈ 1000 × T(encode_manifest), where T >> single-chunk I/O cost
```

Each iteration re-serializes the full manifest. A Byzantine peer achieves the same effect by sending 1000 `/state-sync/chunk` POST requests with `chunk_id = MANIFEST_CHUNK_ID_OFFSET` over QUIC to the serving replica's P2P endpoint.

### Citations

**File:** rs/state_manager/src/state_sync/types.rs (L368-370)
```rust
pub fn encode_manifest(manifest: &Manifest) -> Vec<u8> {
    pb::Manifest::proxy_encode(manifest.clone())
}
```

**File:** rs/state_manager/src/state_sync/types.rs (L430-440)
```rust
pub struct StateSyncMessage {
    pub height: Height,
    pub root_hash: CryptoHashOfState,
    /// Absolute path to the checkpoint root directory.
    pub checkpoint_root: std::path::PathBuf,
    pub meta_manifest: Arc<MetaManifest>,
    /// The manifest containing the summary of the content.
    pub manifest: Manifest,
    pub state_sync_file_group: Arc<FileGroupChunks>,
    pub malicious_flags: MaliciousFlags,
}
```

**File:** rs/state_manager/src/state_sync/types.rs (L498-510)
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
