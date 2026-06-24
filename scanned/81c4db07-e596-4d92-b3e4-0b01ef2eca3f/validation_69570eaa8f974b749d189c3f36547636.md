### Title
Unbounded `inflight_requests` JoinSet + `read_to_end(128MB)` per stream allows a single authenticated Byzantine peer to OOM a replica — (`rs/p2p/quic_transport/src/request_handler.rs`)

---

### Summary

A single subnet peer holding a valid TLS certificate can open `MAX_CONCURRENT_BIDI_STREAMS = 1000` bidi streams on one QUIC connection and drip data on each stream. Because `start_stream_acceptor` spawns one unbounded Tokio task per accepted stream into an uncapped `JoinSet`, and each task calls `recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES)` which accumulates all received bytes into a growing `Vec<u8>`, the attacker can force up to 1000 × 128 MB ≈ 128 GB of heap allocation, OOM-killing the replica process.

---

### Finding Description

**`inflight_requests` has no size cap.**

`start_stream_acceptor` runs an event loop that calls `conn_handle.conn().accept_bi()` and immediately spawns a task for every accepted stream: [1](#0-0) [2](#0-1) 

There is no check on `inflight_requests.len()` before spawning. The code comment at lines 52–56 explicitly acknowledges the risk ("the extreme result of a slow handler is that the stream limit will be reached, hence having buffered up to the stream limit number of messages/requests") but does not account for the memory cost of each buffered message.

**`read_to_end` accumulates all bytes into a heap `Vec`.**

Each spawned task calls: [3](#0-2) 

Quinn's `read_to_end(limit)` allocates a `Vec<u8>` that grows incrementally as bytes arrive from the stream. The `limit` parameter is only a safety cap against a single oversized message — it does not pre-allocate 128 MB. However, as the receiver reads data, QUIC flow-control window updates are sent back to the sender, allowing the sender to push more data. The Vec therefore grows to the full message size over time.

**Flow-control windows limit rate, not total allocation.** [4](#0-3) 

`STREAM_RECEIVE_WINDOW = 4 MB` and `RECEIVE_WINDOW = 200 MB` bound how much unread data can be in-flight at any instant. Because `read_to_end` continuously reads and acknowledges data, the windows slide forward, allowing the sender to keep pushing. The Vec accumulates every byte that has been read — the windows do not cap the total heap allocation.

**Configured limits that enable the attack.** [5](#0-4) [6](#0-5) 

`MAX_MESSAGE_SIZE_BYTES = 128 MB` and `MAX_CONCURRENT_BIDI_STREAMS = 1000` are both production constants. Their product (128 GB) is the theoretical per-connection memory ceiling.

---

### Impact Explanation

An OOM-kill of the replica process terminates consensus participation for that node. If the attacker controls or compromises enough nodes to repeat this against multiple replicas simultaneously (still within the Byzantine fault threshold), subnet liveness is lost. Even against a single replica, the node drops out of consensus until it restarts, degrading subnet throughput and finality latency.

---

### Likelihood Explanation

The precondition is possession of one valid subnet TLS certificate, which corresponds to a single Byzantine subnet node — explicitly within the IC's Byzantine fault model. No governance majority, no threshold key, and no DDoS volume is required. The attack is slow (rate-limited by the 200 MB connection window) but fully deterministic and requires no timing luck. A single malicious or compromised node can execute it against every peer it is connected to.

---

### Recommendation

1. **Cap `inflight_requests`**: Before calling `inflight_requests.spawn(...)`, check `inflight_requests.len() >= MAX_CONCURRENT_BIDI_STREAMS` and either drop the stream or apply back-pressure (e.g., stop calling `accept_bi` until a slot is free). The existing comment at lines 52–56 already identifies the correct fix direction.
2. **Bound per-connection memory**: Track bytes-in-flight per connection and close the connection if the aggregate exceeds a configured limit (e.g., `MAX_CONCURRENT_BIDI_STREAMS × some_per_message_budget`).
3. **Consider streaming deserialization**: Instead of `read_to_end` into a full `Vec`, read the length prefix first and reject streams whose declared size exceeds a per-connection budget.

---

### Proof of Concept

```
1. Attacker (Byzantine subnet node with valid TLS cert) connects to victim replica.
2. Attacker opens 1000 bidi streams simultaneously (within MAX_CONCURRENT_BIDI_STREAMS=1000).
3. On each stream, attacker sends data in 4 MB chunks (respecting STREAM_RECEIVE_WINDOW),
   waiting for window updates before sending the next chunk.
4. start_stream_acceptor spawns 1000 tasks into inflight_requests (no cap check).
5. Each task blocks in recv_stream.read_to_end(128*1024*1024), accumulating bytes into a Vec.
6. As each Vec grows toward 128 MB, total heap pressure approaches 1000 × 128 MB = 128 GB.
7. The OS OOM-killer terminates the replica process before all streams complete.
```

State-machine test: mock a `quinn::Connection` that accepts 1000 streams each yielding `MAX_MESSAGE_SIZE_BYTES - 1` bytes; assert that `start_stream_acceptor` either rejects streams beyond a cap or that process RSS stays below a configured bound.

### Citations

**File:** rs/p2p/quic_transport/src/request_handler.rs (L50-50)
```rust
    let mut inflight_requests: JoinSet<Result<(), P2PError>> = tokio::task::JoinSet::new();
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L72-90)
```rust
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

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L71-74)
```rust
const RECEIVE_WINDOW: VarInt = VarInt::from_u32(200_000_000);
const SEND_WINDOW: u64 = 100_000_000;
const STREAM_RECEIVE_WINDOW: VarInt = VarInt::from_u32(4_000_000);
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
```

**File:** rs/p2p/quic_transport/src/lib.rs (L74-74)
```rust
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```
