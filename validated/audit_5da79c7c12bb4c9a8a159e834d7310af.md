Looking at the actual code, I need to trace both the inbound and outbound attack paths, check the QUIC transport configuration constants, and evaluate whether the flow-control windows actually bound heap allocation.

### Title
Byzantine Subnet Peer Memory Exhaustion via Unbounded `read_to_end` Across 1000 Concurrent QUIC Streams — (`rs/p2p/quic_transport/src/request_handler.rs`)

---

### Summary

A TLS-authenticated Byzantine subnet peer can open the maximum allowed number of bidirectional QUIC streams (1,000) against a victim replica and send up to `MAX_MESSAGE_SIZE_BYTES` (128 MiB) on each. Because the stream acceptor spawns an unbounded `JoinSet` of tasks — each calling `recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES)` — and because QUIC flow-control windows bound only the *rate* of delivery (not the total heap accumulation), the victim can be forced to allocate up to **128 GiB** of heap memory, triggering an OOM crash and dropping the replica out of consensus.

---

### Finding Description

**Constants (all in production code):**

| Constant | Value | File |
|---|---|---|
| `MAX_CONCURRENT_BIDI_STREAMS` | 1,000 | `connection_manager.rs:74` |
| `MAX_MESSAGE_SIZE_BYTES` | 128 MiB | `lib.rs:74` |
| `RECEIVE_WINDOW` (connection) | 200 MB | `connection_manager.rs:71` |
| `STREAM_RECEIVE_WINDOW` (per-stream) | 4 MB | `connection_manager.rs:73` |

**Inbound attack path — the primary vector:**

`start_stream_acceptor` in `request_handler.rs` runs an event loop that calls `conn_handle.conn().accept_bi()` and immediately spawns a task into an **unbounded** `JoinSet` for every accepted stream: [1](#0-0) [2](#0-1) 

Each spawned task calls `read_request`, which calls: [3](#0-2) 

`read_to_end` allocates a `Vec<u8>` that grows continuously as data arrives from the peer. There is no cap on the number of concurrently live tasks in `inflight_requests`.

The developers themselves acknowledge the unbounded buffering risk in a comment immediately above the loop: [4](#0-3) 

**Why QUIC flow control does not bound heap allocation:**

The connection-level `RECEIVE_WINDOW` (200 MB) and per-stream `STREAM_RECEIVE_WINDOW` (4 MB) limit how much data the Byzantine peer can have *in-flight* (sent but not yet acknowledged by the application) at any instant. [5](#0-4) 

However, as each `read_to_end` call reads bytes out of the QUIC receive buffer into its internal `Vec<u8>`, the quinn stack automatically advances the flow-control window, granting the Byzantine peer permission to send more. The flow-control windows therefore pace delivery but do **not** cap the total heap bytes accumulated across all concurrent `read_to_end` calls. The ceiling is:

```
1,000 streams × 128 MiB/stream = 128 GiB
```

**Outbound path (secondary):**

The same `read_to_end(MAX_MESSAGE_SIZE_BYTES)` pattern exists in `ConnectionHandle::rpc()`: [6](#0-5) 

Here the victim opens streams to the Byzantine peer. The Byzantine peer can delay sending the response body, holding all streams open, then flush 128 MiB on each simultaneously. This path requires the victim's upper-layer protocols to issue ~1,000 concurrent RPCs to the same peer, making it less directly attacker-controlled than the inbound path.

---

### Impact Explanation

- Victim replica heap grows to the OS OOM threshold (well below 128 GiB on typical IC hardware).
- The OS OOM-killer terminates the replica process.
- The replica drops out of consensus; if the Byzantine peer repeats the attack against additional replicas, finalization can stall for the subnet.
- A single Byzantine node (below the `f < n/3` fault threshold) is sufficient.

---

### Likelihood Explanation

- The attacker must hold a valid TLS certificate for a subnet node — i.e., be a compromised subnet replica. This is a meaningful prerequisite but is explicitly within the "protocol peer behavior below the consensus fault threshold" scope.
- The attack is slow (paced by the 200 MB connection window) but requires no special timing or race conditions.
- No operator interaction or governance action is needed after the initial compromise.
- The developers' own comment (`request_handler.rs:52-56`) shows awareness of the stream-limit buffering problem, but the memory-amplification consequence (`1,000 × 128 MiB`) is not addressed.

---

### Recommendation

1. **Cap concurrent in-flight stream tasks.** Replace the unbounded `JoinSet` with a semaphore-guarded pool (e.g., `tokio::sync::Semaphore` with a limit well below `MAX_CONCURRENT_BIDI_STREAMS`). Only call `accept_bi` when a permit is available, so QUIC back-pressure naturally limits the Byzantine peer.

2. **Reduce `MAX_MESSAGE_SIZE_BYTES` or enforce it at the stream level before buffering.** Read only a header/length prefix first; reject streams that declare an oversized body before allocating the full buffer.

3. **Decouple `MAX_CONCURRENT_BIDI_STREAMS` from the application-level concurrency limit.** The QUIC limit (1,000) should be a transport-layer ceiling, not the effective application concurrency.

4. **Add a per-connection memory budget.** Track total bytes currently held in `read_to_end` buffers for a given peer and close the connection if the budget is exceeded.

---

### Proof of Concept

A turmoil-based state-machine test:

1. Spin up a victim `QuicTransport` endpoint with the production constants.
2. Create a mock Byzantine peer that performs mutual TLS with a valid (test) certificate.
3. From the Byzantine peer, open 1,000 bidirectional streams.
4. On each stream, write exactly 128 MiB of arbitrary bytes and then call `finish()`.
5. Poll the victim process RSS (via `/proc/self/status` or `jemalloc` stats) after all streams are accepted.
6. Assert RSS remains below a safe bound (e.g., 2 GiB).

Expected result without a fix: RSS grows to the OS OOM threshold and the victim process is killed before the assertion is reached, demonstrating the vulnerability.

### Citations

**File:** rs/p2p/quic_transport/src/request_handler.rs (L50-50)
```rust
    let mut inflight_requests: JoinSet<Result<(), P2PError>> = tokio::task::JoinSet::new();
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L52-56)
```rust
    // The extreme result of a slow handler is that the stream limit will be reach, hence
    // having buffered up to the stream limit number of messages/requests.
    // A better approach will be to use a router implemented as a tower service and accept
    // streams iff the router is ready. Then the actual number of buffered messages is determined
    // by the handlers instead by the underlying implementation.
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

**File:** rs/p2p/quic_transport/src/connection_handle.rs (L115-124)
```rust
        let response_bytes = recv_stream
            .read_to_end(MAX_MESSAGE_SIZE_BYTES)
            .await
            .inspect_err(|err| {
                observe_read_to_end_error(
                    err,
                    "read_to_end",
                    &self.metrics.connection_handle_errors_total,
                )
            })?;
```
