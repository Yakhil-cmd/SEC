Audit Report

## Title
Unbounded `encode_manifest()` Per Chunk Request Enables CPU/Memory Exhaustion on Serving Replica — (`rs/p2p/state_sync_manager/src/routes/chunk.rs`, `rs/state_manager/src/state_sync/types.rs`)

## Summary

Any valid subnet peer can flood the `/state-sync/chunk` endpoint with `ManifestChunk` requests. Each request unconditionally calls `encode_manifest()` — allocating and serializing the full manifest in memory — followed by `zstd::bulk::compress()`, inside an unbounded `spawn_blocking` task. There is no caching of the encoded manifest, no per-peer semaphore, and no concurrency cap on the serving side. With 1000 concurrent QUIC streams (the configured per-connection limit), a Byzantine peer can trigger up to 1000 simultaneous full-manifest allocations and compressions, causing severe memory pressure and blocking thread pool saturation on the targeted replica.

## Finding Description

**Entrypoint**: `state_sync_chunk_handler` in `rs/p2p/state_sync_manager/src/routes/chunk.rs` (L41–74). The handler spawns a blocking task with no concurrency guard:

```rust
let jh = tokio::task::spawn_blocking(
    move || match state.state_sync.chunk(&artifact_id, chunk_id) { ... }
);
```

`StateSyncChunkHandler` holds no semaphore and no per-peer counter (`rs/p2p/state_sync_manager/src/routes/chunk.rs`, L21–39).

The call chain is: `state_sync_chunk_handler` → `spawn_blocking` → `StateSync::chunk` (`rs/state_manager/src/state_sync.rs`, L427–430) → `StateSyncMessage::get_chunk` → `encode_manifest`.

For any `chunk_id >= MANIFEST_CHUNK_ID_OFFSET` (= `1 << 31`, `rs/state_manager/src/state_sync/types.rs`, L123) where the derived index is within `sub_manifest_hashes.len()`, the code at `rs/state_manager/src/state_sync/types.rs` (L498–515) unconditionally executes:

```rust
let encoded_manifest = encode_manifest(&self.manifest); // full alloc every call
```

The encoded manifest is **never cached** in `StateSyncMessage`. For a 5000-canister state it exceeds 1 MiB, producing at least 2 valid sub-manifest chunk IDs.

**Transport-level concurrency**: `MAX_CONCURRENT_BIDI_STREAMS = 1_000` per connection (`rs/p2p/quic_transport/src/connection_manager.rs`, L74). The `start_stream_acceptor` loop in `rs/p2p/quic_transport/src/request_handler.rs` (L50–56) spawns a new task per accepted stream with no inflight cap — the code comment explicitly acknowledges this gap.

**Existing checks are insufficient**: The only guard is the `index < sub_manifest_hashes.len()` bounds check, which a valid peer trivially satisfies using `MANIFEST_CHUNK_ID_OFFSET + 0` or `+1`. There is no rate limit, no semaphore, and no manifest encoding cache anywhere in the serving path.

## Impact Explanation

This is a **High** severity finding matching: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

With 1000 concurrent streams, a Byzantine peer can force up to 1000 simultaneous `encode_manifest()` calls. For a 5 MiB encoded manifest, this is ~5 GiB of concurrent heap allocations plus zstd compression CPU on the serving replica's blocking thread pool (Tokio default: 512 threads). This can cause OOM, blocking thread pool saturation starving other legitimate operations, and the targeted replica falling behind in consensus participation — a single-replica availability impact. The attack is not raw volumetric DDoS; it is a protocol-level amplification exploiting expensive computation per request.

## Likelihood Explanation

The attacker must be a valid subnet peer (below the Byzantine fault threshold), which is an accepted attacker model for ICP bounties. Valid manifest chunk IDs are predictable (`MANIFEST_CHUNK_ID_OFFSET + 0`, `+1`). No special knowledge beyond subnet membership is required. The attack is trivially repeatable and requires no coordination with other nodes.

## Recommendation

1. **Cache the encoded manifest** in `StateSyncMessage` (e.g., as a `once_cell::sync::OnceCell<Vec<u8>>`) so `encode_manifest()` is called at most once per state, not once per chunk request.
2. **Add a per-peer concurrency semaphore** in `StateSyncChunkHandler`, following the pattern already used in `rs/http_endpoints/xnet/src/lib.rs` (L156–168), to cap simultaneous `spawn_blocking` tasks per peer.
3. Return `StatusCode::TOO_MANY_REQUESTS` when the semaphore is exhausted — the client-side parser already handles this status code at `rs/p2p/state_sync_manager/src/routes/chunk.rs` (L129).

## Proof of Concept

1. Spin up a replica with a 5000-canister checkpoint (encoded manifest > 1 MiB, ≥ 2 valid sub-manifest chunks), as demonstrated by the existing test at `rs/state_manager/tests/state_manager.rs` (L3582–3605).
2. From a valid subnet peer, open 1000 concurrent QUIC streams to the serving replica's transport port.
3. On each stream, send a `StateSyncChunkRequest` with `chunk_id = MANIFEST_CHUNK_ID_OFFSET` (= `2_147_483_648`) and the correct `StateSyncArtifactId`.
4. Observe: the serving replica's memory usage spikes by several GiB; blocking thread pool saturates; replica latency increases significantly.

The full call chain is: `state_sync_chunk_handler` → `spawn_blocking` → `StateSync::chunk` → `StateSyncMessage::get_chunk` → `encode_manifest` (full manifest allocation) → `zstd::bulk::compress`.