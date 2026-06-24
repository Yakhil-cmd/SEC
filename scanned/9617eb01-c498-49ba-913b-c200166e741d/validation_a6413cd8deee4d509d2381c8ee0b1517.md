### Title
Unbounded Per-Peer Heap Allocation via Concurrent QUIC Bidi Streams — (`rs/p2p/quic_transport/src/request_handler.rs` + `connection_handle.rs`)

---

### Summary

A Byzantine subnet peer holding a valid TLS-authenticated QUIC connection can exhaust the heap of a victim replica by opening up to `MAX_CONCURRENT_BIDI_STREAMS = 1000` concurrent bidi streams and streaming exactly `MAX_MESSAGE_SIZE_BYTES = 128 MiB` on each. The QUIC flow-control windows (`RECEIVE_WINDOW = 200 MB`, `STREAM_RECEIVE_WINDOW = 4 MB`) do **not** bound application-layer heap growth because `read_to_end` drains the QUIC receive buffer into a `Vec<u8>` while continuously issuing window-update frames, allowing the sender to refill the QUIC buffer. The result is up to **1000 × 128 MiB ≈ 128 GiB** of simultaneous heap allocation, causing an OOM-induced replica process crash.

---

### Finding Description

**Entrypoint — `start_stream_acceptor` (request_handler.rs)**

The stream acceptor loop accepts every incoming bidi stream without backpressure and immediately spawns a Tokio task: [1](#0-0) 

The code itself contains a comment acknowledging the unbounded buffering risk: [2](#0-1) 

**Per-stream allocation — `read_request` (request_handler.rs)**

Each spawned task calls `read_to_end(MAX_MESSAGE_SIZE_BYTES)`, which allocates a `Vec<u8>` that grows up to 128 MiB per stream: [3](#0-2) 

**The 128 MiB cap:** [4](#0-3) 

**The 1000-stream cap and flow-control windows:** [5](#0-4) 

**Why QUIC flow control does not bound heap usage:**

`STREAM_RECEIVE_WINDOW = 4 MB` and `RECEIVE_WINDOW = 200 MB` limit how much data the sender may have *in-flight in the QUIC layer* at any instant. However, Quinn's `read_to_end` works by:

1. Reading chunks from the QUIC receive buffer into a `Vec<u8>`.
2. Issuing `MAX_STREAM_DATA` / `MAX_DATA` window-update frames as each chunk is consumed from the QUIC buffer.
3. Repeating until the stream FIN is received.

As a result, the QUIC receive buffer stays bounded by the configured windows (~4 MB per stream, ~200 MB total), but the application-level `Vec<u8>` accumulates all data up to 128 MiB per stream. The flow-control windows pace delivery; they do not cap the total heap allocation.

**Worst-case math:**

| Parameter | Value |
|---|---|
| `MAX_CONCURRENT_BIDI_STREAMS` | 1,000 |
| `MAX_MESSAGE_SIZE_BYTES` | 128 MiB |
| Max simultaneous heap | **~128 GiB** |

Even at 100 concurrent streams the allocation reaches ~12.8 GiB, well above typical replica RAM.

---

### Impact Explanation

An OOM kill of the replica process causes that node to drop out of consensus participation. The attack requires only one Byzantine subnet peer (within the standard BFT fault assumption of up to `f` Byzantine nodes out of `3f+1`). The crash is non-volumetric — it requires no flooding, only a single connection sending large but protocol-legal messages. The impact is scoped to the single targeted replica.

---

### Likelihood Explanation

The precondition is a valid TLS-authenticated QUIC connection, meaning the attacker must be a legitimate subnet member. This is within the IC Byzantine fault model. The attack is deterministic and requires no timing or race conditions — simply opening 1000 streams and writing 128 MiB on each is sufficient. No special privileges beyond subnet membership are required.

---

### Recommendation

1. **Enforce a per-connection in-flight byte budget.** Track total bytes currently held in `read_to_end` Vecs across all active streams for a given peer. Reject or stall new streams once the budget is exceeded.
2. **Apply backpressure before accepting streams.** The code comment at lines 52–56 already identifies the correct fix: gate `accept_bi` on router readiness so that the number of concurrently buffered messages is bounded by handler throughput, not by `MAX_CONCURRENT_BIDI_STREAMS`.
3. **Reduce `MAX_MESSAGE_SIZE_BYTES` or `MAX_CONCURRENT_BIDI_STREAMS`.** The product of these two constants must fit within the replica's memory budget even in the worst case. At present `1000 × 128 MiB = 128 GiB` far exceeds any realistic heap limit.
4. **Stream data incrementally.** Replace `read_to_end` with a chunked reader that enforces a hard limit on the total bytes buffered across all concurrent streams from a single peer.

---

### Proof of Concept

```
// Peer-side harness (pseudocode)
let conn = quic_connect(victim_addr, tls_cert).await;  // legitimate subnet TLS cert

let mut handles = vec![];
for _ in 0..1000 {
    let conn = conn.clone();
    handles.push(tokio::spawn(async move {
        let (mut tx, _rx) = conn.open_bi().await.unwrap();
        // Send exactly MAX_MESSAGE_SIZE_BYTES = 128 MiB of zeros
        tx.write_all(&vec![0u8; 128 * 1024 * 1024]).await.unwrap();
        tx.finish().unwrap();
        // Do NOT read the response — keep the stream open to hold the allocation
    }));
}
futures::future::join_all(handles).await;
// Victim replica OOM-killed before all streams complete
```

Each of the 1000 spawned tasks on the victim calls `read_to_end(128 MiB)`. The QUIC `STREAM_RECEIVE_WINDOW = 4 MB` paces delivery but does not prevent the `Vec<u8>` from growing to 128 MiB per task as window-update frames are issued. Total heap pressure: ~128 GiB.

### Citations

**File:** rs/p2p/quic_transport/src/request_handler.rs (L52-56)
```rust
    // The extreme result of a slow handler is that the stream limit will be reach, hence
    // having buffered up to the stream limit number of messages/requests.
    // A better approach will be to use a router implemented as a tower service and accept
    // streams iff the router is ready. Then the actual number of buffered messages is determined
    // by the handlers instead by the underlying implementation.
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L57-90)
```rust
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
