Looking at the actual constants and code flow to evaluate this claim precisely.

### Title
Unbounded `inflight_requests` JoinSet + `read_to_end(128 MiB)` per stream enables per-connection heap exhaustion — (`rs/p2p/quic_transport/src/request_handler.rs`)

---

### Summary

A Byzantine subnet peer holding a valid TLS certificate can open `MAX_CONCURRENT_BIDI_STREAMS` (1,000) bidirectional QUIC streams on a single connection and send up to `MAX_MESSAGE_SIZE_BYTES` (128 MiB) on each. The `start_stream_acceptor` loop spawns an unbounded tokio task per accepted stream into `inflight_requests`, and each task calls `read_to_end(MAX_MESSAGE_SIZE_BYTES)`, which accumulates all received bytes into a growing `Vec<u8>`. QUIC flow control limits the instantaneous in-flight data but does **not** cap the total accumulated heap allocation across all concurrent `Vec` buffers. The theoretical ceiling is 1,000 × 128 MiB ≈ **128 GiB** of heap, sufficient to OOM a replica.

---

### Finding Description

**Constants (all confirmed in source):**

| Constant | Value | Location |
|---|---|---|
| `MAX_MESSAGE_SIZE_BYTES` | 128 MiB | `lib.rs:74` |
| `MAX_CONCURRENT_BIDI_STREAMS` | 1,000 | `connection_manager.rs:74` |
| `STREAM_RECEIVE_WINDOW` | 4 MB | `connection_manager.rs:73` |
| `RECEIVE_WINDOW` (connection) | 200 MB | `connection_manager.rs:71` |

**Attack path:**

1. Byzantine peer (valid TLS cert, subnet member) establishes one QUIC connection to a victim replica.
2. It opens 1,000 bidirectional streams simultaneously (permitted by `MAX_CONCURRENT_BIDI_STREAMS`).
3. `start_stream_acceptor` accepts each stream and spawns a task into `inflight_requests` — an **unbounded** `JoinSet` with no admission control: [1](#0-0) [2](#0-1) 

4. Each spawned task calls `read_request`, which calls `recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES)`: [3](#0-2) 

Quinn's `read_to_end` reads chunks from the QUIC stream buffer into a `Vec<u8>` that grows until EOF. As each chunk is consumed from the QUIC buffer, a window update is sent to the sender, allowing more data to flow in. The `Vec<u8>` accumulates **all** received bytes — it is not bounded by the flow control window.

**Why flow control does not prevent the allocation:**

- `STREAM_RECEIVE_WINDOW = 4 MB` limits how much unread data can be buffered **inside the QUIC layer** per stream at any instant.
- `RECEIVE_WINDOW = 200 MB` limits the total in-flight data across all streams at any instant.
- Neither window limits the **total size of the `Vec<u8>` buffers** that `read_to_end` accumulates over time. As data is read from the QUIC buffer, the window opens and more data flows in, growing the Vec further.
- With 1,000 concurrent streams each eventually receiving 128 MiB, the aggregate `Vec` allocation reaches **128 GiB** — flow control only determines the rate, not the ceiling.

The developers acknowledge the design gap in a comment: [4](#0-3) 

The comment notes that up to the stream limit of messages can be buffered, but does not account for the memory cost of 1,000 × 128 MiB. [5](#0-4) 

---

### Impact Explanation

An OOM crash terminates the replica process, removing it from consensus participation. If `f` Byzantine nodes each target a distinct victim replica and crash it, the number of live honest replicas drops below `2f+1`, breaking finalization safety. Even a single crashed replica degrades fault tolerance headroom.

---

### Likelihood Explanation

The attacker must be a legitimate subnet member (Byzantine node within the `f`-fault threshold) with a valid TLS certificate. This is within the explicit threat model of Byzantine fault-tolerant consensus. No external network access, no key compromise, and no governance majority is required — only a single established QUIC connection from a Byzantine peer.

---

### Recommendation

1. **Bound `inflight_requests`**: Replace the unbounded `JoinSet` with a semaphore-gated or bounded channel so that at most `N` concurrent stream tasks are active per connection (e.g., `N = 10–50`), applying back-pressure by not calling `accept_bi` when the limit is reached.
2. **Reduce `MAX_CONCURRENT_BIDI_STREAMS`**: Lower from 1,000 to a value consistent with actual protocol usage patterns.
3. **Reduce `MAX_MESSAGE_SIZE_BYTES` or enforce per-connection memory accounting**: Track total bytes in-flight across all `read_to_end` calls for a given peer and reject/reset streams that would exceed a per-peer memory budget.
4. **Consider streaming deserialization**: Instead of `read_to_end` into a full `Vec`, use chunked reads with an explicit size prefix so the allocation can be rejected before the data arrives.

---

### Proof of Concept

```
1. Attacker node (valid TLS cert, subnet member) connects to victim replica.
2. Open 1,000 bidi streams via QUIC (within MAX_CONCURRENT_BIDI_STREAMS=1000).
3. On each stream, send a valid protobuf-framed HttpRequest with a 128 MiB body.
4. Flow control (STREAM_RECEIVE_WINDOW=4 MB) throttles each stream, but
   read_to_end on the victim side accumulates received bytes into a Vec<u8>
   and sends window updates, allowing the sender to continue.
5. After all 1,000 Vecs grow to 128 MiB each, victim heap usage = ~128 GiB → OOM.
6. Assert: replica process exits before all streams complete.
```

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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L173-178)
```rust
    let request_bytes = recv_stream
        .read_to_end(MAX_MESSAGE_SIZE_BYTES)
        .await
        .inspect_err(|err| {
            observe_read_to_end_error(err, "read_to_end", &metrics.request_handle_errors_total)
        })?;
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L71-75)
```rust
const RECEIVE_WINDOW: VarInt = VarInt::from_u32(200_000_000);
const SEND_WINDOW: u64 = 100_000_000;
const STREAM_RECEIVE_WINDOW: VarInt = VarInt::from_u32(4_000_000);
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);
```
