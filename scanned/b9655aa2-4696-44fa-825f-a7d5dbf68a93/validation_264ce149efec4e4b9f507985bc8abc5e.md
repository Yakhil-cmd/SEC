After reading all relevant production code, here is my assessment:

---

### Title
Unbounded Per-Connection Stream Task Spawning Enables Memory Exhaustion via Byzantine Subnet Peer — (`rs/p2p/quic_transport/src/request_handler.rs`)

### Summary

A Byzantine subnet peer with an established authenticated connection can open `MAX_CONCURRENT_BIDI_STREAMS` (1,000) simultaneous bidirectional QUIC streams. Each accepted stream unconditionally spawns a new Tokio task into an unbounded `JoinSet`. Each task calls `read_to_end(MAX_MESSAGE_SIZE_BYTES)` which allocates up to 128 MiB per stream. With 1,000 concurrent streams, this totals up to ~128 GiB of heap allocation, exhausting replica memory and crashing the process.

### Finding Description

**Confirmed constants:**

- `MAX_CONCURRENT_BIDI_STREAMS = 1_000` [1](#0-0) 
- `MAX_MESSAGE_SIZE_BYTES = 128 * 1024 * 1024` (128 MiB) [2](#0-1) 

**Unbounded task spawning in `start_stream_acceptor`:**

The event loop holds an unbounded `JoinSet` called `inflight_requests`. Every accepted bidi stream immediately spawns a new task with no backpressure, no concurrency cap, and no memory accounting:

```rust
let mut inflight_requests: JoinSet<Result<(), P2PError>> = tokio::task::JoinSet::new();
``` [3](#0-2) 

```rust
inflight_requests.spawn(
    metrics.request_task_monitor.instrument(
        handle_bi_stream(...)
    )
);
``` [4](#0-3) 

**The code itself acknowledges this design gap in a comment:**

> "The extreme result of a slow handler is that the stream limit will be reached, hence having buffered up to the stream limit number of messages/requests." [5](#0-4) 

**Per-task heap allocation in `read_request`:**

Each spawned task calls `read_to_end(MAX_MESSAGE_SIZE_BYTES)`, which allocates a `Vec<u8>` growing up to 128 MiB:

```rust
let request_bytes = recv_stream
    .read_to_end(MAX_MESSAGE_SIZE_BYTES)
    .await
``` [6](#0-5) 

**QUIC flow control does not prevent this:**

The transport config sets `STREAM_RECEIVE_WINDOW = 4 MB` and `RECEIVE_WINDOW = 200 MB` (connection-level). [7](#0-6) 

These are sliding windows: as `read_to_end` reads data into its buffer, it advances the flow control window, allowing the sender to push more data. The attacker can slowly fill each stream's buffer up to 128 MiB. The connection-level 200 MB window throttles throughput but does not cap total accumulated memory across 1,000 long-lived tasks.

**Authentication gate:**

The TLS configuration restricts connections to authenticated subnet peers only. [8](#0-7) 

This means the attacker must be a legitimate (but Byzantine) subnet node — a single compromised node, which is below the consensus fault threshold.

### Impact Explanation

A single Byzantine subnet peer can exhaust the heap of a targeted honest replica (~128 GiB theoretical maximum), causing an OOM crash. This halts consensus participation for the affected node. By selectively targeting specific replicas, a Byzantine peer could reduce the honest-node count toward the fault threshold, weakening the subnet's Byzantine fault tolerance guarantees.

### Likelihood Explanation

- Requires only one compromised subnet node (below fault threshold, within scope).
- The attack is straightforward: open 1,000 streams, send large bodies slowly.
- No rate limiting, no per-connection memory cap, no backpressure exists.
- The code comment explicitly acknowledges the buffering risk.
- Replica nodes typically have 256–512 GiB RAM; 128 GiB is a realistic OOM trigger.

### Recommendation

1. **Cap concurrent inflight tasks per connection**: Replace the unbounded `JoinSet` with a semaphore-guarded or bounded channel, so that `accept_bi` blocks when the in-flight task count reaches a safe limit (e.g., 32–64).
2. **Apply a per-connection memory budget**: Track total bytes being read across all active tasks for a connection and reject new streams when the budget is exceeded.
3. **Reduce `MAX_CONCURRENT_BIDI_STREAMS`**: Lower from 1,000 to a value consistent with actual P2P protocol needs.
4. **Implement streaming request parsing**: Instead of `read_to_end` (full buffering), use incremental reads so memory is bounded by what the handler has consumed, not what the peer has sent.

### Proof of Concept

```
1. Attacker controls a legitimate subnet node (Byzantine, below fault threshold).
2. Attacker's node has an established QUIC connection to the victim replica
   (present in peer_map after TLS handshake).
3. Attacker opens 1,000 bidirectional QUIC streams simultaneously.
4. On each stream, attacker slowly sends 128 MiB of valid protobuf-framed data
   (respecting the 4 MB stream window, advancing it as the receiver reads).
5. start_stream_acceptor spawns 1,000 tasks, each blocked in read_to_end(128 MiB).
6. Each task accumulates up to 128 MiB in its Vec<u8> buffer.
7. Total heap: ~128 GiB → OOM → replica process crash.
8. Consensus participation halts for the victim node.
```

### Citations

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L71-73)
```rust
const RECEIVE_WINDOW: VarInt = VarInt::from_u32(200_000_000);
const SEND_WINDOW: u64 = 100_000_000;
const STREAM_RECEIVE_WINDOW: VarInt = VarInt::from_u32(4_000_000);
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L74-74)
```rust
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L208-215)
```rust
    let rustls_server_config = tls_config
        .server_config(
            SomeOrAllNodes::Some(BTreeSet::new()),
            registry_client.get_latest_version(),
        )
        .expect(
            "The rustls server config must be locally available, otherwise transport can't start.",
        );
```

**File:** rs/p2p/quic_transport/src/lib.rs (L74-74)
```rust
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```

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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L173-175)
```rust
    let request_bytes = recv_stream
        .read_to_end(MAX_MESSAGE_SIZE_BYTES)
        .await
```
