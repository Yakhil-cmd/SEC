Audit Report

## Title
Unbounded Concurrent `encode_manifest` in `state_sync_chunk_handler` Enables CPU/Memory Exhaustion by a Byzantine Peer — (File: `rs/p2p/state_sync_manager/src/routes/chunk.rs`)

## Summary
The `state_sync_chunk_handler` dispatches every incoming `StateSyncChunkRequest` unconditionally into a `tokio::task::spawn_blocking` task with no per-peer concurrency limit. When the requested chunk ID falls in the manifest range, `StateSyncMessage::get_chunk` calls `encode_manifest` on every invocation with no caching, allocating ≥100 MiB per call. A Byzantine subnet peer can open up to 1,000 concurrent QUIC streams (the configured limit), triggering up to 512 simultaneous blocking tasks each allocating ≥100 MiB, potentially exhausting available RAM and saturating CPU on the serving replica.

## Finding Description

**Entry point — `state_sync_chunk_handler`**

Every incoming chunk request is dispatched unconditionally into `tokio::task::spawn_blocking` with no semaphore, no per-peer counter, and no `TOO_MANY_REQUESTS` guard: [1](#0-0) 

**Full manifest re-encoding on every manifest chunk request**

Inside `StateSyncMessage::get_chunk`, the `ManifestChunk` arm unconditionally calls `encode_manifest(&self.manifest)` — serializing the entire manifest — then slices out the requested sub-manifest piece. There is no caching of the encoded bytes between calls: [2](#0-1) 

`DEFAULT_CHUNK_SIZE` is confirmed as 1 MiB: [3](#0-2) 

**QUIC stream limit is the only guard — and it is 1,000**

The QUIC transport sets `MAX_CONCURRENT_BIDI_STREAMS = VarInt::from_u32(1_000)` per connection: [4](#0-3) 

The request handler spawns a new Tokio task for every accepted stream with no further throttling. The code itself acknowledges this gap: [5](#0-4) 

**Manifest size at scale**

The test suite explicitly asserts that for a large state the encoded manifest exceeds 100 MiB: [6](#0-5) 

**`TOO_MANY_REQUESTS` is already handled client-side but never sent server-side**

The client already handles `TOO_MANY_REQUESTS` gracefully as `DownloadChunkError::Overloaded`, but the server never emits it: [7](#0-6) 

## Impact Explanation

With 1,000 concurrent streams (the QUIC per-connection limit), the Tokio blocking thread pool (default cap: 512 threads) will run up to 512 simultaneous `encode_manifest` calls. Each allocates ≥100 MiB. Peak heap pressure reaches ≥50 GiB of live allocations, plus zstd compression buffers on top. This can trigger OOM on the serving replica or cause sustained CPU saturation, degrading or killing that replica's ability to participate in consensus and serve other traffic. This matches the allowed impact: **High ($2,000–$10,000) — Application/platform-level DoS, crash, or subnet availability impact not based on raw volumetric DDoS.**

## Likelihood Explanation

The attacker must be a valid subnet peer authenticated via mutual TLS — a Byzantine node already admitted to the subnet. This is a realistic threat within the IC fault model (up to `f` Byzantine nodes tolerated). The attack requires no special knowledge beyond the advertised state height/hash (broadcast every 5 seconds by the victim). The chunk ID `MANIFEST_CHUNK_ID_OFFSET + 0` is always valid as long as the replica has a checkpoint, which is the normal operating condition. The attack is repeatable indefinitely since there is no application-layer rate limit.

## Recommendation

1. **Cache the encoded manifest** inside `StateSyncMessage` (e.g., using `once_cell::sync::OnceCell<Vec<u8>>`) so `encode_manifest` is called at most once per state, not once per chunk request.
2. **Add a per-peer concurrency semaphore** in `StateSyncChunkHandler` (e.g., `Arc<Semaphore>` keyed by `NodeId`) limiting concurrent `spawn_blocking` tasks per peer to a small value (e.g., 4–8).
3. **Return `StatusCode::TOO_MANY_REQUESTS`** when the semaphore cannot be acquired — the client already handles this gracefully via `DownloadChunkError::Overloaded`.

## Proof of Concept

```
1. Replica A has a checkpoint with 5,000 canisters → encoded manifest > 100 MiB.
2. Byzantine peer B opens a QUIC connection to A (authenticated via subnet TLS).
3. B sends 1,000 concurrent StateSyncChunkRequest { id: <A's state id>, chunk_id: MANIFEST_CHUNK_ID_OFFSET + 0 }.
4. A's state_sync_chunk_handler spawns 1,000 spawn_blocking tasks (no guard).
5. Up to 512 run simultaneously; each calls encode_manifest() → ~100 MiB alloc → zstd compress.
6. Peak heap: ~50 GiB → OOM kill or severe memory pressure on A.
7. Repeat indefinitely; A cannot rate-limit B at the application layer.

Reproducible as a deterministic integration test: construct a StateSyncMessage with a
dummy_file_table_and_chunk_table()-sized manifest (as used in compatibility.rs), spin up a
local QuicTransport pair, and fire 1,000 concurrent chunk requests for MANIFEST_CHUNK_ID_OFFSET+0,
asserting that the serving side's memory usage grows proportionally and/or that requests are not
throttled.
```

### Citations

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

**File:** rs/state_manager/src/state_sync/types.rs (L95-95)
```rust
pub const DEFAULT_CHUNK_SIZE: u32 = 1 << 20; // 1 MiB.
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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L52-56)
```rust
    // The extreme result of a slow handler is that the stream limit will be reach, hence
    // having buffered up to the stream limit number of messages/requests.
    // A better approach will be to use a router implemented as a tower service and accept
    // streams iff the router is ready. Then the actual number of buffered messages is determined
    // by the handlers instead by the underlying implementation.
```

**File:** rs/state_manager/src/manifest/tests/compatibility.rs (L592-595)
```rust
    assert!(
        encode_manifest(&manifest_v2).len() > 100 * DEFAULT_CHUNK_SIZE as usize,
        "The encoded manifest is supposed to be larger than 100 MiB."
    );
```
