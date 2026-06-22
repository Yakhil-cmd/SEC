### Title
Byzantine Peer Can Exhaust `MAX_CONCURRENT_BIDI_STREAMS` Budget and Permanently Block Inbound Stream Acceptance — (`rs/p2p/quic_transport/src/request_handler.rs`)

---

### Summary

A Byzantine subnet peer with an established QUIC connection can open all 1,000 permitted bidirectional streams and hold each one open indefinitely by never sending a FIN frame. Because `read_request` calls `recv_stream.read_to_end(...)` with no timeout, all 1,000 `inflight_requests` tasks block permanently. The QUIC layer stops advertising new stream credit, `accept_bi()` never returns a new stream, and the victim replica's inbound P2P stream acceptance on that connection is permanently frozen — with no automatic connection close triggered.

---

### Finding Description

**Constant and configuration**

`MAX_CONCURRENT_BIDI_STREAMS` is set to 1,000 and applied to the server-side transport config: [1](#0-0) [2](#0-1) 

**The blocking read with no timeout**

Every accepted bidi stream spawns a task that immediately calls `read_to_end`, which blocks until the peer sends a FIN or a connection-level error occurs: [3](#0-2) 

Quinn's `read_to_end` has no built-in deadline. If the Byzantine peer opens a stream, writes 1 byte, and never sends FIN, this future never resolves. The `RecvStream` is never dropped, so the QUIC stream slot is never returned to the peer's credit window.

**The event loop has no escape hatch**

The `start_stream_acceptor` loop selects on `inflight_requests.join_next()` and `conn_handle.conn().accept_bi()`. Once 1,000 tasks are blocked in `read_to_end`, the QUIC layer stops advertising stream credit; `accept_bi()` never yields a new stream. The loop is alive but permanently stalled on new inbound streams: [4](#0-3) 

The developers themselves acknowledge the stream-limit risk in a comment but leave it unmitigated: [5](#0-4) 

**No connection-level recovery**

`IDLE_TIMEOUT` (5 s) only fires when the connection is completely idle. A Byzantine peer sending periodic keep-alive packets keeps the connection alive indefinitely. The connection manager has no "stream-exhaustion watchdog" that would close the connection: [6](#0-5) 

---

### Impact Explanation

The victim replica permanently loses the ability to accept any new inbound QUIC streams from the Byzantine peer on that connection. All push-based P2P artifact delivery from that peer is blocked. Because the connection is not closed, the connection manager does not attempt reconnection, so the blockage persists until a topology change or external event forces a connection teardown. This constitutes a constrained subnet availability issue for the targeted replica's P2P layer.

---

### Likelihood Explanation

The attacker must be an authenticated subnet peer (TLS-verified). A single Byzantine node below the consensus fault threshold can execute this with minimal resources: open 1,000 streams, write 1 byte each, never send FIN, send QUIC keep-alives. No volumetric traffic is required. The attack is deterministic and locally reproducible.

---

### Recommendation

Add a per-stream read deadline wrapping `read_to_end`, e.g.:

```rust
tokio::time::timeout(READ_TIMEOUT, recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES)).await
```

If the timeout fires, the task returns an error, the `RecvStream` is dropped, the stream slot is reclaimed, and the QUIC layer re-advertises credit. Alternatively, enforce an application-level cap on `inflight_requests.len()` and stop calling `accept_bi()` when the cap is reached, then close the connection if the cap is held for too long.

---

### Proof of Concept

State-machine test outline:

1. Establish an authenticated QUIC connection to the victim's `start_stream_acceptor`.
2. Open 1,000 bidi streams from the Byzantine peer; on each stream write 1 byte but never call `finish()`.
3. Assert that `accept_bi()` on the victim side never returns a new stream (stream credit exhausted).
4. Assert that the connection is **not** closed after `IDLE_TIMEOUT * 2` (keep-alives prevent it).
5. Assert that a legitimate 1,001st stream open attempt from the Byzantine peer is blocked by the QUIC layer (`STREAMS_BLOCKED` frame sent, no `accept_bi` delivery on victim).

Expected result without fix: victim's inbound stream acceptance is permanently frozen; connection stays open; `inflight_requests` holds 1,000 stalled tasks indefinitely.

### Citations

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L74-75)
```rust
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L78-82)
```rust
const KEEP_ALIVE_INTERVAL: Duration = Duration::from_secs(1);
/// Timeout after which quic marks connections as broken. This timeout is used to detect connections
/// that were not explicitly closed. I.e replica crash
const IDLE_TIMEOUT: Duration = Duration::from_secs(5);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L225-226)
```rust
        .max_concurrent_bidi_streams(MAX_CONCURRENT_BIDI_STREAMS)
        .max_concurrent_uni_streams(MAX_CONCURRENT_UNI_STREAMS);
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L52-56)
```rust
    // The extreme result of a slow handler is that the stream limit will be reach, hence
    // having buffered up to the stream limit number of messages/requests.
    // A better approach will be to use a router implemented as a tower service and accept
    // streams iff the router is ready. Then the actual number of buffered messages is determined
    // by the handlers instead by the underlying implementation.
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L57-109)
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
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L172-178)
```rust
) -> Result<Request<Body>, P2PError> {
    let request_bytes = recv_stream
        .read_to_end(MAX_MESSAGE_SIZE_BYTES)
        .await
        .inspect_err(|err| {
            observe_read_to_end_error(err, "read_to_end", &metrics.request_handle_errors_total)
        })?;
```
