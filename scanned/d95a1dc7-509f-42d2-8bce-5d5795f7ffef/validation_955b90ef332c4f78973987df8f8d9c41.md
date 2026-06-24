### Title
Unbounded Per-Stream Memory Accumulation via Concurrent P2P QUIC Streams Causes Replica OOM - (File: `rs/p2p/quic_transport/src/request_handler.rs`)

---

### Summary

The IC QUIC transport layer (`QuicTransport`) allows any authenticated subnet peer to open up to 1,000 concurrent bidirectional QUIC streams per connection. Each stream is handled by a dedicated tokio task that calls `read_to_end(MAX_MESSAGE_SIZE_BYTES)` where `MAX_MESSAGE_SIZE_BYTES = 128 MiB`. A malicious subnet peer can exploit this to cause the receiving replica to accumulate up to **128 GiB** of heap memory across all concurrent stream buffers, leading to OOM and a replica crash — a Denial-of-Service condition directly analogous to the Ethereum go-ethereum P2P memory exhaustion vulnerability.

---

### Finding Description

**Root cause — `MAX_MESSAGE_SIZE_BYTES` is 128 MiB:** [1](#0-0) 

```rust
/// On purpose the value is big, otherwise there is risk of not processing important consensus messages.
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```

**Root cause — up to 1,000 concurrent bidirectional streams are accepted per connection:** [2](#0-1) 

```rust
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
```

**Root cause — each accepted stream spawns an unbounded tokio task that calls `read_to_end(MAX_MESSAGE_SIZE_BYTES)`:** [3](#0-2) 

The `start_stream_acceptor` loop accepts every incoming bidirectional stream and immediately spawns a task into an unbounded `JoinSet`: [4](#0-3) 

Each spawned task calls `read_request`, which calls: [5](#0-4) 

```rust
let request_bytes = recv_stream
    .read_to_end(MAX_MESSAGE_SIZE_BYTES)
    .await
```

`quinn::RecvStream::read_to_end` allocates a `Vec<u8>` that grows dynamically as data arrives from the peer, up to the supplied limit. It does **not** pre-allocate the full limit, but it **does** accumulate all received bytes into heap memory before returning. The QUIC flow-control windows (`STREAM_RECEIVE_WINDOW = 4 MiB`, `RECEIVE_WINDOW = 200 MiB`) only throttle the *rate* of data transfer; they do not cap the *total* heap memory accumulated across all concurrent `read_to_end` calls over time. [6](#0-5) 

As the receiver reads data from each stream and sends QUIC window-update frames, the sender is permitted to send more data. Over time, each of the 1,000 concurrent stream tasks can accumulate up to 128 MiB in its `Vec<u8>`, for a theoretical total of **128 GiB** of heap allocation from a single malicious peer connection.

The same pattern is present on the response-collection path: [7](#0-6) 

```rust
let body = axum::body::to_bytes(body, MAX_MESSAGE_SIZE_BYTES).await?;
```

---

### Impact Explanation

A malicious subnet peer opens 1,000 concurrent bidirectional QUIC streams (the protocol-enforced maximum) and slowly streams data on each, staying within the QUIC flow-control windows to avoid triggering a connection reset. The receiving replica spawns 1,000 tokio tasks, each accumulating data in a heap `Vec<u8>`. As the connection window (200 MiB) is consumed and replenished via window updates, the Vecs grow continuously. Once total heap allocation exceeds the replica's available RAM, the OS OOM-killer terminates the replica process, causing a node outage. Because the attack is sustained and gradual, it evades simple rate-limit heuristics. A crashed replica cannot participate in consensus, degrading subnet liveness.

---

### Likelihood Explanation

The attacker must be an authenticated subnet peer (TLS-verified via the IC registry). This places the attacker below the consensus fault threshold — within the stated scope. No admin key, governance majority, or external oracle is required. The attack requires only a standard QUIC client that opens the maximum number of streams and sends data at a controlled rate. The `MAX_CONCURRENT_BIDI_STREAMS = 1,000` and `MAX_MESSAGE_SIZE_BYTES = 128 MiB` constants are both reachable by any subnet peer without any special privilege.

---

### Recommendation

1. **Reduce `MAX_MESSAGE_SIZE_BYTES`** to a value consistent with the actual largest legitimate P2P message (e.g., the largest consensus artifact). The comment in `lib.rs` cites summary blocks for 40-node subnets as the motivation for 128 MiB; a tighter per-handler limit should be enforced instead of a single global ceiling.

2. **Enforce per-peer memory accounting**: Track total bytes currently buffered across all in-flight `read_to_end` calls for a given peer connection. Reject or back-pressure new streams when the per-peer budget is exceeded.

3. **Bound the `JoinSet`**: Cap the number of concurrently inflight request tasks per connection at a value lower than `MAX_CONCURRENT_BIDI_STREAMS`, so that slow or large messages on existing streams prevent new streams from being accepted until capacity is freed.

4. **Apply per-handler message size limits** at the router layer rather than relying on the single global `MAX_MESSAGE_SIZE_BYTES` constant shared across all protocol handlers.

---

### Proof of Concept

```
Attacker (malicious subnet peer, TLS-authenticated):

1. Establish a QUIC connection to the victim replica (authenticated via IC TLS).
2. Open 1,000 bidirectional streams (MAX_CONCURRENT_BIDI_STREAMS limit).
3. On each stream, send a protobuf-encoded HttpRequest whose body is
   127 MiB of arbitrary bytes, paced to stay within the QUIC flow-control
   windows (STREAM_RECEIVE_WINDOW=4 MiB, RECEIVE_WINDOW=200 MiB).
4. The victim spawns 1,000 tokio tasks, each calling:
       recv_stream.read_to_end(134_217_728 /* 128 MiB */).await
   Each task's Vec<u8> grows as data arrives and window updates are sent.
5. After ~200 MiB of data is in flight at any moment, the connection window
   is replenished as the receiver reads data into the Vecs.
6. Over time, each of the 1,000 Vecs grows toward 128 MiB.
   At 1 Gbps, ~64 GiB of heap is allocated in ~8 minutes, triggering OOM.
7. The replica process is killed; the node drops out of consensus.
```

Key constants confirming exploitability: [8](#0-7) [9](#0-8) [5](#0-4)

### Citations

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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L50-50)
```rust
    let mut inflight_requests: JoinSet<Result<(), P2PError>> = tokio::task::JoinSet::new();
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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L173-178)
```rust
    let request_bytes = recv_stream
        .read_to_end(MAX_MESSAGE_SIZE_BYTES)
        .await
        .inspect_err(|err| {
            observe_read_to_end_error(err, "read_to_end", &metrics.request_handle_errors_total)
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
