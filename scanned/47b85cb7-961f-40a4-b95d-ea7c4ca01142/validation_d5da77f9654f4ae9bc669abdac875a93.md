### Title
Unbounded `read_to_end()` in QUIC Stream Handler Enables Stream Exhaustion DoS by Malicious Subnet Peer - (File: `rs/p2p/quic_transport/src/request_handler.rs`)

---

### Summary

The QUIC transport layer's incoming request handler calls `recv_stream.read_to_end()` without any per-stream timeout. A malicious subnet peer (below the consensus fault threshold) can open the maximum number of bidirectional QUIC streams (`MAX_CONCURRENT_BIDI_STREAMS = 1,000`) and never send a FIN, causing 1,000 Tokio tasks per connection to block indefinitely in `read_to_end()`. This exhausts the `inflight_requests` JoinSet and starves legitimate P2P consensus message processing on the victim node.

---

### Finding Description

In `rs/p2p/quic_transport/src/request_handler.rs`, the function `read_request()` reads the full request body from a QUIC `RecvStream` using `read_to_end()` with no deadline or timeout:

```rust
async fn read_request(
    mut recv_stream: RecvStream,
    metrics: &QuicTransportMetrics,
) -> Result<Request<Body>, P2PError> {
    let request_bytes = recv_stream
        .read_to_end(MAX_MESSAGE_SIZE_BYTES)
        .await          // <-- blocks indefinitely; no timeout
        ...
``` [1](#0-0) 

This is called from `handle_bi_stream()`, which is spawned as an unbounded Tokio task for every accepted bidirectional QUIC stream in `start_stream_acceptor()`:

```rust
inflight_requests.spawn(
    metrics.request_task_monitor.instrument(
        handle_bi_stream(peer_id, conn_handle.conn_id(), metrics.clone(), router.clone(), send_stream, bi_rx)
    )
);
``` [2](#0-1) 

The code itself acknowledges the risk in a comment:

> "The extreme result of a slow handler is that the stream limit will be reached, hence having buffered up to the stream limit number of messages/requests." [3](#0-2) 

The QUIC transport is configured with `MAX_CONCURRENT_BIDI_STREAMS = 1,000` per connection: [4](#0-3) 

The connection-level `IDLE_TIMEOUT` is 5 seconds, but this only fires when **no packets** are received. A malicious peer can continuously send QUIC keepalives (the local endpoint also sends them every `KEEP_ALIVE_INTERVAL = 1s`) to keep the connection alive while all 1,000 streams remain open and never send FIN. [5](#0-4) 

---

### Impact Explanation

A single malicious subnet node (below the consensus fault threshold) can:

1. Establish a TLS-authenticated QUIC connection to a victim replica node.
2. Open 1,000 bidirectional streams (the QUIC-enforced maximum per connection).
3. Send partial data on each stream but never send FIN.
4. Each stream spawns a Tokio task permanently blocked in `read_to_end()`.
5. The `inflight_requests` JoinSet fills with 1,000 stuck tasks.
6. Legitimate P2P consensus messages from honest peers compete for Tokio runtime threads with 1,000 permanently blocked tasks.

This degrades or halts the victim node's ability to process consensus artifacts (block proposals, notarizations, finalizations) delivered over the QUIC transport, potentially causing the node to fall behind in consensus and affecting subnet liveness.

---

### Likelihood Explanation

The QUIC transport is the primary P2P layer for IC consensus. Any subnet peer (authenticated via mutual TLS) can open streams. A single compromised or malicious node below the fault threshold can execute this attack without any special privileges beyond subnet membership. The attack requires only opening streams and withholding FIN — a trivial network-level action. No cryptographic material or governance access is needed.

---

### Recommendation

Add a per-stream read timeout wrapping `read_to_end()` in `read_request()`:

```rust
async fn read_request(
    recv_stream: RecvStream,
    metrics: &QuicTransportMetrics,
) -> Result<Request<Body>, P2PError> {
    let request_bytes = tokio::time::timeout(
        READ_REQUEST_TIMEOUT,
        recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES),
    )
    .await
    .map_err(|_| P2PError::from("read_request timed out".to_string()))??;
    ...
```

A reasonable `READ_REQUEST_TIMEOUT` value should be derived from the expected maximum message transmission time (e.g., `MAX_MESSAGE_SIZE_BYTES / min_expected_bandwidth + margin`). Additionally, consider bounding the size of `inflight_requests` per connection and shedding load when the limit is reached.

---

### Proof of Concept

1. A malicious subnet node (with valid TLS credentials) connects to a victim node's QUIC endpoint.
2. It opens 1,000 bidirectional streams in rapid succession (up to `MAX_CONCURRENT_BIDI_STREAMS`).
3. On each stream, it sends a single byte of data (to prevent the stream from being immediately reset) but never sends FIN.
4. The victim node's `start_stream_acceptor` spawns 1,000 tasks, each blocked in `recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES).await`.
5. The malicious node sends QUIC keepalive packets every second to prevent the `IDLE_TIMEOUT` from closing the connection.
6. The victim node's Tokio runtime is saturated with 1,000 permanently blocked tasks, starving legitimate consensus message handlers. [6](#0-5) [1](#0-0) [7](#0-6)

### Citations

**File:** rs/p2p/quic_transport/src/request_handler.rs (L43-110)
```rust
pub async fn start_stream_acceptor(
    log: ReplicaLogger,
    peer_id: NodeId,
    conn_handle: ConnectionHandle,
    metrics: QuicTransportMetrics,
    router: Router,
) {
    let mut inflight_requests: JoinSet<Result<(), P2PError>> = tokio::task::JoinSet::new();
    let mut quic_metrics_scrape = tokio::time::interval(QUIC_METRIC_SCRAPE_INTERVAL);
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
                    }
                    Err(err) => {
                        info!(
                            log,
                            "Exiting request handler event loop due to conn error {:?}",
                            err.to_string()
                        );
                        observe_conn_error(&err, "accept_bi", &metrics.request_handle_errors_total);
                        break;
                    }
                }
            },
            _ = conn_handle.conn().accept_uni() => {},
            _ = conn_handle.conn().read_datagram() => {},
            _ = quic_metrics_scrape.tick() => {
                metrics.collect_quic_connection_stats(conn_handle.conn(), &peer_id);
            }
        }
    }
}
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

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L74-82)
```rust
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);

/// Interval of quic heartbeats. They are only sent if the connection is idle for more than 1sec.
const KEEP_ALIVE_INTERVAL: Duration = Duration::from_secs(1);
/// Timeout after which quic marks connections as broken. This timeout is used to detect connections
/// that were not explicitly closed. I.e replica crash
const IDLE_TIMEOUT: Duration = Duration::from_secs(5);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
```
