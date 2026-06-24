Audit Report

## Title
Byzantine Peer Memory Exhaustion via Unbounded Concurrent Response Buffering in `handle_bi_stream` — (`rs/p2p/quic_transport/src/request_handler.rs`)

## Summary
A Byzantine subnet peer can open up to 1,000 concurrent bidirectional QUIC streams, send valid artifact-fetch requests on each, and cause the serving replica to allocate up to `MAX_CONCURRENT_BIDI_STREAMS × MAX_MESSAGE_SIZE_BYTES = 1,000 × 128 MiB = 128 GiB` of heap memory simultaneously. The root cause is that `to_response_bytes` fully materializes the response body into a contiguous `Vec<u8>` with no cancellation guard, and the `stopped_fut` that could abort early is consumed by the preceding `tokio::select!` and never re-polled during the buffering phase. With realistic artifact sizes (5–8 MiB), 1,000 concurrent streams produce 5–8 GiB of simultaneous allocation, sufficient to OOM a replica node.

## Finding Description

**Root cause — `stopped_fut` dropped after `select!` exits:**

In `handle_bi_stream`, `stopped_fut` is created from `send_stream.stopped()` and polled exclusively inside the `select!`: [1](#0-0) 

Once the handler arm (`svc`) wins the race, `stopped_fut` is dropped. From that point forward there is no check for whether the peer has sent a STOP_SENDING frame or stopped reading. The code then unconditionally calls: [2](#0-1) 

**Root cause — `to_response_bytes` allocates up to 128 MiB with no interleaved cancellation:** [3](#0-2) 

The limit is: [4](#0-3) 

**Root cause — 1,000 concurrent streams permitted and each spawned as an independent task with no backpressure:** [5](#0-4) 

This is applied directly to the QUIC transport config: [6](#0-5) 

Each accepted stream is immediately spawned with no semaphore or backpressure: [7](#0-6) 

**Real handlers produce large responses.** The artifact downloader's `rpc_handler` serializes a full artifact from the validated pool into `Bytes`: [8](#0-7) 

The state sync chunk handler returns chunks up to `MAX_CHUNK_SIZE = 8 MiB`: [9](#0-8) 

**Why existing checks fail:** The `STREAM_RECEIVE_WINDOW = 4 MiB` per stream limits how much data the peer must accept, but it does not prevent the *server* from allocating the full response buffer before attempting `write_all`. The `IDLE_TIMEOUT = 5 s` eventually closes the connection, but the damage window is sufficient for an OOM crash. [10](#0-9) 

## Impact Explanation

A single Byzantine subnet peer can crash a replica node via OOM by holding 1,000 streams open and withholding flow-control credits. This constitutes **application/platform-level DoS causing replica crash and subnet availability impact**, matching the High ($2,000–$10,000) bounty impact tier. Even with realistic artifact sizes (5–8 MiB per stream), 1,000 concurrent streams produce 5–8 GiB of simultaneous heap allocation. At the theoretical maximum (128 MiB per stream), peak usage reaches 128 GiB. Either scenario is sufficient to OOM a typical replica node. A crashed replica reduces subnet fault tolerance and, if repeated against multiple nodes, can halt consensus.

## Likelihood Explanation

The attacker must be an authenticated subnet peer — a Byzantine node below the consensus fault threshold. A single compromised node suffices; no threshold majority is required. The attack requires only standard QUIC operations: opening streams and withholding flow-control credits (not reading responses). No timing precision is needed. The attack is repeatable: after the `IDLE_TIMEOUT = 5 s` closes the connection, the Byzantine node can reconnect and repeat.

## Recommendation

1. **Re-arm cancellation around `to_response_bytes`**: After the `select!` exits, wrap `to_response_bytes` in a new `select!` that also polls `send_stream.stopped()`, so that a STOP_SENDING frame or peer disconnect aborts the allocation immediately.
2. **Stream the response incrementally**: Replace the full-body buffer with incremental writes so that QUIC flow control naturally limits in-flight memory to `STREAM_RECEIVE_WINDOW` (4 MiB) per stream rather than `MAX_MESSAGE_SIZE_BYTES` (128 MiB).
3. **Bound concurrent in-flight tasks per peer**: Apply a semaphore in `start_stream_acceptor` to cap the number of simultaneously executing handler tasks per connection, decoupling the QUIC stream limit from the application-level memory budget.

## Proof of Concept

```
1. Byzantine peer connects (authenticated via TLS as a subnet node).
2. Opens 1,000 bidirectional streams simultaneously
   (permitted by MAX_CONCURRENT_BIDI_STREAMS = 1,000).
3. On each stream, sends a valid serialized artifact-fetch request
   for a known large artifact (e.g., a consensus artifact or state-sync chunk).
4. Does NOT send STOP_SENDING during handler execution
   (so stopped_fut does not fire inside the select!).
5. Handler completes → to_response_bytes calls
   axum::body::to_bytes(body, 128 MiB), allocating up to 128 MiB per task.
6. Peer stops reading responses (exhausts STREAM_RECEIVE_WINDOW = 4 MiB per stream)
   → write_all blocks for each of the 1,000 tasks.
7. All 1,000 allocations are live simultaneously.
   Worst case: 1,000 × 128 MiB = 128 GiB heap.
   Realistic case (5–8 MiB artifacts): 5–8 GiB heap → OOM.
8. Replica is killed by the OS OOM killer.

Reproducible test plan:
- Write an integration test using PocketIC or a local replica harness.
- Spawn a mock peer that opens 1,000 bidi streams, sends valid
  artifact-fetch requests, and never reads the response stream.
- Assert that the serving replica's RSS exceeds a threshold
  (e.g., 4 GiB) within the IDLE_TIMEOUT window, or that the
  process is killed with SIGKILL/OOM.
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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L128-136)
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
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L149-155)
```rust
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

**File:** rs/p2p/quic_transport/src/lib.rs (L74-74)
```rust
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

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L225-226)
```rust
        .max_concurrent_bidi_streams(MAX_CONCURRENT_BIDI_STREAMS)
        .max_concurrent_uni_streams(MAX_CONCURRENT_UNI_STREAMS);
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
