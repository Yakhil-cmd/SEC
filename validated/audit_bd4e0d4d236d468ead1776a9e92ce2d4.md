Audit Report

## Title
Unbounded Per-Peer Heap Allocation via Concurrent QUIC Bidi Streams — (`rs/p2p/quic_transport/src/request_handler.rs`)

## Summary
A Byzantine subnet peer holding a valid TLS-authenticated QUIC connection can open up to `MAX_CONCURRENT_BIDI_STREAMS = 1,000` concurrent bidi streams and write `MAX_MESSAGE_SIZE_BYTES = 128 MiB` on each. The stream acceptor spawns an unbounded number of Tokio tasks without backpressure, and each task calls `read_to_end(MAX_MESSAGE_SIZE_BYTES)`, allocating a `Vec<u8>` that grows to 128 MiB per stream. QUIC flow-control windows pace delivery but do not cap application-layer heap growth, yielding up to ~128 GiB of simultaneous heap allocation and an OOM-induced replica crash.

## Finding Description
**Stream acceptor — no backpressure (`request_handler.rs` L52–90):**
The `start_stream_acceptor` loop accepts every incoming bidi stream and immediately spawns a Tokio task via `inflight_requests.spawn(...)`. There is no check on the number of in-flight tasks before accepting the next stream. The code itself contains a comment at L52–56 acknowledging this: *"The extreme result of a slow handler is that the stream limit will be reached, hence having buffered up to the stream limit number of messages/requests."* [1](#0-0) 

**Per-stream allocation — `read_request` (`request_handler.rs` L169–178):**
Each spawned task calls `recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES)`, which allocates a `Vec<u8>` that grows up to 128 MiB per stream before the FIN is received. [2](#0-1) 

**Constants (`lib.rs` L74, `connection_manager.rs` L71–75):**
`MAX_MESSAGE_SIZE_BYTES = 128 MiB`, `MAX_CONCURRENT_BIDI_STREAMS = 1,000`, `STREAM_RECEIVE_WINDOW = 4 MB`, `RECEIVE_WINDOW = 200 MB`. [3](#0-2) [4](#0-3) 

**Why QUIC flow control does not bound heap usage:**
`STREAM_RECEIVE_WINDOW = 4 MB` limits how much data the sender may have in-flight in the QUIC layer at any instant. Quinn's `read_to_end` reads chunks from the QUIC receive buffer into the `Vec<u8>` and issues `MAX_STREAM_DATA` window-update frames as each chunk is consumed, allowing the sender to refill the QUIC buffer. The QUIC buffer stays bounded (~4 MB per stream), but the application-level `Vec<u8>` accumulates all data up to 128 MiB per stream. Flow control paces delivery; it does not cap total heap allocation.

**Worst-case math:**

| Parameter | Value |
|---|---|
| `MAX_CONCURRENT_BIDI_STREAMS` | 1,000 |
| `MAX_MESSAGE_SIZE_BYTES` | 128 MiB |
| Max simultaneous heap | ~128 GiB |

## Impact Explanation
An OOM kill of the replica process causes that node to drop out of consensus participation, constituting an application/platform-level DoS and subnet availability impact not based on raw volumetric DDoS. This matches the **High** bounty impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation
The precondition is a valid TLS-authenticated QUIC connection, meaning the attacker must be a legitimate subnet member — within the IC Byzantine fault model (up to `f` Byzantine nodes out of `3f+1`). The attack is deterministic, requires no timing or race conditions, and is repeatable: simply opening 1,000 streams and writing 128 MiB on each is sufficient. No special privileges beyond subnet membership are required.

## Recommendation
1. **Apply backpressure before accepting streams.** Gate `accept_bi` on router readiness (as the existing comment at L52–56 already identifies as the correct fix), so the number of concurrently buffered messages is bounded by handler throughput, not by `MAX_CONCURRENT_BIDI_STREAMS`.
2. **Enforce a per-connection in-flight byte budget.** Track total bytes currently held in `read_to_end` Vecs across all active streams for a given peer. Reject or stall new streams once the budget is exceeded.
3. **Reduce the product `MAX_MESSAGE_SIZE_BYTES × MAX_CONCURRENT_BIDI_STREAMS`.** This product must fit within the replica's memory budget in the worst case. The current product (128 GiB) far exceeds any realistic heap limit.
4. **Replace `read_to_end` with a chunked reader** that enforces a hard limit on total bytes buffered across all concurrent streams from a single peer.

## Proof of Concept
```rust
// Byzantine peer pseudocode (legitimate subnet TLS cert required)
let conn = quic_connect(victim_addr, tls_cert).await;
let mut handles = vec![];
for _ in 0..1000 {
    let conn = conn.clone();
    handles.push(tokio::spawn(async move {
        let (mut tx, _rx) = conn.open_bi().await.unwrap();
        // Write exactly MAX_MESSAGE_SIZE_BYTES = 128 MiB
        tx.write_all(&vec![0u8; 128 * 1024 * 1024]).await.unwrap();
        tx.finish().unwrap();
        // Do NOT read the response — keep the stream open to hold the allocation
    }));
}
futures::future::join_all(handles).await;
// Victim replica OOM-killed: each of the 1000 tasks called read_to_end(128 MiB)
// QUIC STREAM_RECEIVE_WINDOW=4MB paces delivery but does not prevent Vec growth
// Total heap pressure: ~128 GiB
```
A deterministic integration test can be written using a local Quinn endpoint with a self-signed certificate, opening 1,000 bidi streams and writing 128 MiB on each, then asserting the replica process exits with OOM or that memory usage exceeds a threshold.

### Citations

**File:** rs/p2p/quic_transport/src/request_handler.rs (L52-90)
```rust
    // The extreme result of a slow handler is that the stream limit will be reach, hence
    // having buffered up to the stream limit number of messages/requests.
    // A better approach will be to use a router implemented as a tower service and accept
    // streams iff the router is ready. Then the actual number of buffered messages is determined
    // by the handlers instead by the underlying implementation.
    loop {
        tokio::select! {
            Some(completed_request) = inflight_requests.join_next() => {
                match completed_request {
                    Ok(res) => {
                        let _ = res.inspect_err(|err| info!(every_n_seconds => 60, log, "{:?}", err));
                    }
                    Err(err) => {
                        // Cancelling tasks is ok. Panicking tasks are not.
                        if err.is_panic() {
                            std::panic::resume_unwind(err.into_panic());
                        }
                    }
                }
            },
            bi = conn_handle.conn().accept_bi() => {
                match bi {
                    Ok((bi_tx, bi_rx)) => {
                        let send_stream = ResetStreamOnDrop::new(
                            bi_tx,
                            metrics.connection_handle_incoming_streams_total.clone()
                        );
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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L169-178)
```rust
async fn read_request(
    mut recv_stream: RecvStream,
    metrics: &QuicTransportMetrics,
) -> Result<Request<Body>, P2PError> {
    let request_bytes = recv_stream
        .read_to_end(MAX_MESSAGE_SIZE_BYTES)
        .await
        .inspect_err(|err| {
            observe_read_to_end_error(err, "read_to_end", &metrics.request_handle_errors_total)
        })?;
```

**File:** rs/p2p/quic_transport/src/lib.rs (L72-74)
```rust
/// On purpose the value is big, otherwise there is risk of not processing important consensus messages.
/// E.g. summary blocks generated by the consensus protocol for 40 node subnet can be bigger than 5MB.
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L71-75)
```rust
const RECEIVE_WINDOW: VarInt = VarInt::from_u32(200_000_000);
const SEND_WINDOW: u64 = 100_000_000;
const STREAM_RECEIVE_WINDOW: VarInt = VarInt::from_u32(4_000_000);
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);
```
