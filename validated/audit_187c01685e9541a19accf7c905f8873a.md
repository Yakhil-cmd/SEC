The code is present and readable. Let me now do a precise analysis of the exact execution path.

The vulnerability claim has three components to verify:
1. Can `to_response_bytes` buffer a large response after the handler completes?
2. Is the `stopped_fut` guard absent after the `select!` exits?
3. Can 1000 concurrent streams be opened by a single peer?

### Title
Byzantine Peer Memory Exhaustion via Unbuffered Response Allocation in `to_response_bytes` — (`rs/p2p/quic_transport/src/request_handler.rs`)

---

### Summary

A Byzantine subnet peer can open up to 1,000 concurrent bidirectional QUIC streams, request large artifacts on each, and cause the serving replica to allocate up to `MAX_CONCURRENT_BIDI_STREAMS × MAX_MESSAGE_SIZE_BYTES = 1,000 × 128 MiB = 128 GiB` of heap memory simultaneously. The root cause is that `to_response_bytes` fully materializes the response body into a `Vec<u8>` with no interleaved cancellation check, and the `stopped_fut` guard that could abort early is consumed by the preceding `tokio::select!` and never re-polled during the buffering phase.

---

### Finding Description

In `handle_bi_stream`, the execution sequence is:

```
tokio::select! {
    response = svc => ...,          // handler runs; stopped_fut polled here
    stopped = stopped_fut => return // early exit if STOP_SENDING arrives during handler
};
// ← stopped_fut is consumed here; no cancellation guard below this line
let response_bytes = to_response_bytes(response).await?;   // allocates up to 128 MiB
send_stream.write_all(&response_bytes).await?;             // only now touches the wire
``` [1](#0-0) 

The `stopped_fut` future is created from `send_stream.stopped()` and is polled exclusively inside the `select!`. Once the handler arm wins, `stopped_fut` is dropped. From that point until `write_all` returns, there is no check for whether the peer has sent a STOP_SENDING frame or simply stopped reading.

`to_response_bytes` calls `axum::body::to_bytes(body, MAX_MESSAGE_SIZE_BYTES)`, which collects the entire streaming body into a contiguous allocation bounded only by `MAX_MESSAGE_SIZE_BYTES = 128 MiB`: [2](#0-1) [3](#0-2) 

The QUIC configuration permits 1,000 concurrent bidirectional streams per connection: [4](#0-3) 

Each accepted stream is immediately spawned as an independent tokio task with no backpressure: [5](#0-4) 

Real handlers that produce large responses exist. The artifact downloader's `rpc_handler` serializes a full artifact from the validated pool into `Bytes` and returns it directly: [6](#0-5) 

The state sync chunk handler returns chunks up to `MAX_CHUNK_SIZE = 8 MiB` per request: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

A Byzantine peer opens 1,000 streams and sends valid artifact-fetch requests on each. The handlers run concurrently (one tokio task per stream). Each task reaches `to_response_bytes` and allocates up to 128 MiB before attempting `write_all`. If the peer does not read responses (exhausting the `STREAM_RECEIVE_WINDOW = 4 MiB` per stream), `write_all` blocks and all 1,000 allocations are live simultaneously. Peak heap usage: **1,000 × 128 MiB = 128 GiB**. Even with realistic artifact sizes (5–8 MiB), 1,000 concurrent streams produce 5–8 GiB of simultaneous allocation, sufficient to OOM a replica node. The `IDLE_TIMEOUT = 5 s` eventually closes the connection, but the damage window is sufficient for a crash. [9](#0-8) 

---

### Likelihood Explanation

The attacker must be an authenticated subnet peer — a Byzantine node below the consensus fault threshold. This is explicitly within scope per the question's stated attack surface ("protocol peer behavior below the consensus fault threshold"). No threshold majority is required; a single compromised node suffices. The attack requires only standard QUIC stream opening and withholding flow-control credits, both of which are normal protocol operations. No timing precision is needed: the attacker simply opens 1,000 streams, sends valid requests, and stops reading.

---

### Recommendation

1. **Re-check `stopped` before buffering**: After the `select!` exits, call `send_stream.stopped().now_or_never()` (or re-arm a `select!` around `to_response_bytes`) to abort if the peer has already sent STOP_SENDING.
2. **Stream the response to the wire**: Replace the full-body buffer with incremental writes so that QUIC flow control naturally limits in-flight memory to `STREAM_RECEIVE_WINDOW` (4 MiB) per stream rather than `MAX_MESSAGE_SIZE_BYTES`.
3. **Bound concurrent in-flight tasks per peer**: Apply a semaphore in `start_stream_acceptor` to cap the number of simultaneously executing handler tasks per connection, decoupling the QUIC stream limit from the application-level memory budget.

---

### Proof of Concept

```
1. Byzantine peer connects (authenticated via TLS as a subnet node).
2. Opens 1,000 bidirectional streams simultaneously.
3. On each stream, sends a valid /{artifact}/rpc request for a known large artifact.
4. Does NOT send STOP_SENDING during handler execution
   (so stopped_fut does not fire inside the select!).
5. Handler completes → to_response_bytes allocates up to 128 MiB per task.
6. Peer stops reading (exhausts STREAM_RECEIVE_WINDOW = 4 MiB) →
   write_all blocks for each of the 1,000 tasks.
7. All 1,000 allocations are live simultaneously → up to 128 GiB heap usage.
8. Replica OOMs or is killed by the OS.
```

### Citations

**File:** rs/p2p/quic_transport/src/request_handler.rs (L79-90)
```rust
                        inflight_requests.spawn(
                            metrics.request_task_monitor.instrument(
                                handle_bi_stream(
                                    peer_id,
                                    conn_handle.conn_id(),
                                    metrics.clone(),
                                    router.clone(),
                                    send_stream,
                                    bi_rx
                                )
                            )
                        );
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L128-155)
```rust
    let stopped_fut = send_stream.stopped();
    let response = tokio::select! {
        response = svc => response.expect("Infallible"),
        stopped = stopped_fut => {
            return Ok(stopped.map(|_| ()).inspect_err(|err| {
                observe_stopped_error(err, "request_handler", &metrics.request_handle_errors_total)
            })?);
        }
    };

    // Record application level errors.
    if !response.status().is_success() {
        metrics
            .request_handle_errors_total
            .with_label_values(&[STREAM_TYPE_BIDI, ERROR_TYPE_APP])
            .inc();
    }

    // We can ignore the errors because if both peers follow the protocol an errors will only occur
    // if the other peer has closed the connection. In this case `accept_bi` in the peer event
    // loop will close this connection.
    let response_bytes = to_response_bytes(response).await?;
    send_stream
        .write_all(&response_bytes)
        .await
        .inspect_err(|err| {
            observe_write_error(err, "write_all", &metrics.request_handle_errors_total);
        })?;
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L210-214)
```rust
async fn to_response_bytes(response: Response<Body>) -> Result<Vec<u8>, P2PError> {
    let (parts, body) = response.into_parts();
    // Check for axum error in body
    // TODO: Think about this. What is the error that can happen here?
    let body = axum::body::to_bytes(body, MAX_MESSAGE_SIZE_BYTES).await?;
```

**File:** rs/p2p/quic_transport/src/lib.rs (L72-74)
```rust
/// On purpose the value is big, otherwise there is risk of not processing important consensus messages.
/// E.g. summary blocks generated by the consensus protocol for 40 node subnet can be bigger than 5MB.
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L71-81)
```rust
const RECEIVE_WINDOW: VarInt = VarInt::from_u32(200_000_000);
const SEND_WINDOW: u64 = 100_000_000;
const STREAM_RECEIVE_WINDOW: VarInt = VarInt::from_u32(4_000_000);
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);

/// Interval of quic heartbeats. They are only sent if the connection is idle for more than 1sec.
const KEEP_ALIVE_INTERVAL: Duration = Duration::from_secs(1);
/// Timeout after which quic marks connections as broken. This timeout is used to detect connections
/// that were not explicitly closed. I.e replica crash
const IDLE_TIMEOUT: Duration = Duration::from_secs(5);
```

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L55-72)
```rust
async fn rpc_handler<Artifact: PbArtifact>(
    State(pool): State<ValidatedPoolReaderRef<Artifact>>,
    payload: Bytes,
) -> Result<Bytes, StatusCode> {
    let jh = tokio::task::spawn_blocking(move || {
        let id: Artifact::Id =
            Artifact::PbId::proxy_decode(&payload).map_err(|_| StatusCode::BAD_REQUEST)?;
        let artifact = pool
            .read()
            .unwrap()
            .get(&id)
            .ok_or(StatusCode::NO_CONTENT)?;
        Ok::<_, StatusCode>(Bytes::from(Artifact::PbMessage::proxy_encode(artifact)))
    });
    let bytes = jh.await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)??;

    Ok(bytes)
}
```

**File:** rs/p2p/state_sync_manager/src/routes/chunk.rs (L19-19)
```rust
const MAX_CHUNK_SIZE: usize = 8 * 1024 * 1024;
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
