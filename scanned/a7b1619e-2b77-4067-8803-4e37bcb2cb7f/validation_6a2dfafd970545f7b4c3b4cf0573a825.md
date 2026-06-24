Looking at the actual production code to verify each claim in the question.

All facts are confirmed from the production code. Here is the assessment:

---

### Title
Unbounded `inflight_requests` JoinSet Enables Per-Connection OOM via 1000 × 128 MB `read_to_end` Allocations — (`rs/p2p/quic_transport/src/request_handler.rs`)

### Summary

A single authenticated Byzantine subnet peer can open `MAX_CONCURRENT_BIDI_STREAMS = 1000` simultaneous bidi streams on one connection and slowly drip data on each, causing `start_stream_acceptor` to hold 1000 concurrent Tokio tasks in an unbounded `inflight_requests` JoinSet, each blocking in `read_to_end(MAX_MESSAGE_SIZE_BYTES)` and accumulating up to 128 MB in a heap-allocated `Vec<u8>`. Total heap pressure: up to 128 GB → OOM kill of the replica process.

### Finding Description

**Confirmed constants (all production code):**

| Constant | Value | File |
|---|---|---|
| `MAX_CONCURRENT_BIDI_STREAMS` | 1 000 | `connection_manager.rs:74` |
| `MAX_MESSAGE_SIZE_BYTES` | 128 × 1024 × 1024 | `lib.rs:74` |
| `STREAM_RECEIVE_WINDOW` | 4 MB | `connection_manager.rs:73` |
| `RECEIVE_WINDOW` (connection) | 200 MB | `connection_manager.rs:71` |

**The unbounded JoinSet** — `start_stream_acceptor` creates `inflight_requests` with no capacity cap: [1](#0-0) 

Every accepted bidi stream unconditionally spawns a new task into it: [2](#0-1) 

Each spawned task calls `read_to_end(MAX_MESSAGE_SIZE_BYTES)`, which accumulates the entire stream payload into a single heap `Vec<u8>` before returning: [3](#0-2) 

**Why QUIC flow control does not prevent this:**

`STREAM_RECEIVE_WINDOW = 4 MB` and `RECEIVE_WINDOW = 200 MB` limit how much data can be *in-flight* (sent but not yet consumed by the application) at any instant. However, `read_to_end` *consumes* data from the QUIC receive buffer into the growing `Vec<u8>` as it arrives. Each consumption advances the flow control window, permitting the sender to push more data. The windows pace the *rate* of arrival but place no bound on the *total* memory accumulated across all 1 000 concurrent `Vec<u8>` buffers. The attacker simply drips data slowly, staying within the 200 MB connection window at each moment, while each Vec grows toward 128 MB over time.

**The developers partially acknowledge the backpressure gap** in the comment at `request_handler.rs:52–56`, noting that "the extreme result of a slow handler is that the stream limit will be reached, hence having buffered up to the stream limit number of messages/requests" — but the comment does not account for the 128 MB per-message size multiplier. [4](#0-3) 

The README explicitly lists "P2P fairness and resource protection — preventing resource exhaustion by any single peer" as a transport requirement, which this implementation fails to satisfy. [5](#0-4) 

### Impact Explanation

A single Byzantine subnet node (one valid TLS certificate, within the single-fault-tolerance model) can OOM-kill any replica it connects to. Replica OOM → consensus node loss → subnet liveness degradation proportional to the number of nodes attacked simultaneously. The transport layer is supposed to be resilient to Byzantine peers; this invariant is broken.

### Likelihood Explanation

The precondition is one compromised subnet node (or a node operator acting maliciously). This is within the "protocol peer behavior below the consensus fault threshold" scope. The attack requires no special tooling beyond a QUIC client that opens 1 000 streams and drips data — achievable with a patched `quinn` client. The attack is slow (bounded by the 200 MB connection window) but persistent and requires no interaction from the victim beyond having an active connection.

### Recommendation

1. **Cap `inflight_requests`**: Enforce a maximum JoinSet size (e.g., equal to `MAX_CONCURRENT_BIDI_STREAMS`). When the cap is reached, stop calling `accept_bi` until a task completes — this is the backpressure approach the developer comment already identifies as the correct fix.
2. **Per-connection memory accounting**: Track total bytes currently held in in-progress `read_to_end` calls per connection and close the connection if it exceeds a configured threshold (e.g., 256 MB).
3. **Reduce `MAX_MESSAGE_SIZE_BYTES`**: 128 MB is very large for P2P transport messages. Reducing it to a value consistent with actual protocol message sizes (e.g., 5–10 MB) dramatically reduces the amplification factor.

### Proof of Concept

State-machine test: mock QUIC connection that opens 1 000 bidi streams, each sending `MAX_MESSAGE_SIZE_BYTES - 1` bytes in 4 MB chunks (respecting `STREAM_RECEIVE_WINDOW`). Assert that process RSS stays below a configured limit (e.g., 512 MB). The test will fail because `inflight_requests` will hold 1 000 tasks each with a growing `Vec<u8>`, and total RSS will grow toward 128 GB.

The exact call sequence from the question maps directly to the production code:
- Step 1: `conn_handle.conn().accept_bi()` at `request_handler.rs:72` — no stream count check before spawning
- Step 2: `inflight_requests.spawn(handle_bi_stream(...))` at `request_handler.rs:79` — unbounded
- Step 3: `recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES)` at `request_handler.rs:173–174` — allocates up to 128 MB per task
- Step 4: 1 000 tasks × 128 MB = 128 GB [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/p2p/quic_transport/src/request_handler.rs (L50-56)
```rust
    let mut inflight_requests: JoinSet<Result<(), P2PError>> = tokio::task::JoinSet::new();
    let mut quic_metrics_scrape = tokio::time::interval(QUIC_METRIC_SCRAPE_INTERVAL);
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

**File:** rs/p2p/quic_transport/README.adoc (L31-34)
```text
| P2P fairness and resource protection
| Ensures fair resource allocation in a P2P network where all peers are equal, preventing resource exhaustion by any single peer.
| Enforce https://en.wikipedia.org/wiki/Flow_control_(data)[flow control] to limit the number of connections and in-flight messages per peer, ensuring resource usage stays within allocated boundaries.

```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L71-75)
```rust
const RECEIVE_WINDOW: VarInt = VarInt::from_u32(200_000_000);
const SEND_WINDOW: u64 = 100_000_000;
const STREAM_RECEIVE_WINDOW: VarInt = VarInt::from_u32(4_000_000);
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);
```

**File:** rs/p2p/quic_transport/src/lib.rs (L72-74)
```rust
/// On purpose the value is big, otherwise there is risk of not processing important consensus messages.
/// E.g. summary blocks generated by the consensus protocol for 40 node subnet can be bigger than 5MB.
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```
