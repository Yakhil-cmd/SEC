### Title
Missing Connection Read Timeout on XNet HTTP Endpoint Enables Resource Exhaustion by Malicious Protocol Peer - (File: rs/http_endpoints/xnet/src/lib.rs)

### Summary
The XNet inter-subnet HTTP endpoint (`XNetEndpoint`) accepts TCP connections and spawns an unbounded number of Tokio tasks per connection, but applies **no timeout** on either the TLS handshake or subsequent HTTP serving. A malicious registered IC node (protocol peer) can open many connections and stall indefinitely at the TLS handshake phase, exhausting Tokio task and file-descriptor resources on the target replica node, disrupting cross-subnet message routing.

### Finding Description

In `rs/http_endpoints/xnet/src/lib.rs`, the `start_server` function accepts connections in a loop and spawns a new `tokio::spawn` task per connection:

```rust
tokio::spawn(async move {
    let http = hyper_util::server::conn::auto::Builder::new(...);
    // ...
    let tls_acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(server_config));
    match tls_acceptor.accept(stream).await {   // ← NO TIMEOUT
        Ok(tls_stream) => {
            let conn = http.serve_connection(io, hyper_service);
            if let Err(err) = conn.await { ... } // ← NO TIMEOUT
        }
        ...
    };
});
```

There is no timeout wrapping `tls_acceptor.accept(stream).await` and no `TimeoutStream` applied to the raw TCP stream before or after TLS. A peer that opens a TCP connection but never sends a TLS `ClientHello` (or sends it byte-by-byte) will hold the spawned task open indefinitely.

This is in direct contrast to the **public HTTP endpoint** (`rs/http_endpoints/public/src/lib.rs`), which correctly applies a `read_timeout` at two levels: an initial `timeout(read_timeout, stream.peek(&mut b))` before TLS, and a `tokio_io_timeout::TimeoutStream` wrapping the stream afterward.

The `XNET_ENDPOINT_MAX_CONCURRENT_REQUESTS` semaphore (value: 4) only limits concurrent *processed requests*, not concurrent *accepted connections*. Each stalled connection consumes a Tokio task and a file descriptor regardless of the semaphore.

The metrics HTTP endpoint (`rs/http_endpoints/metrics/src/lib.rs`) has a related gap: it uses `axum::serve(tcp_listener, metrics_service)` with no per-connection read timeout, only a per-request tower timeout layer. However, the metrics endpoint is typically not externally reachable.

### Impact Explanation

An attacker controlling a registered IC node (protocol peer) can open a large number of TCP connections to the XNet endpoint of a target replica node and stall each at the TLS handshake phase. This causes:

1. **Unbounded Tokio task accumulation** — each accepted connection spawns a task that never completes.
2. **File descriptor exhaustion** — each open TCP socket consumes an OS file descriptor.
3. **XNet routing disruption** — the target node's XNet endpoint becomes unable to serve legitimate cross-subnet stream slice requests from other subnets, stalling message routing and potentially delaying finality of cross-subnet calls.

### Likelihood Explanation

The XNet endpoint is protected by nftables rules that restrict inbound connections to IPs of nodes registered in the IC registry. The attacker must therefore be a registered IC node acting maliciously (below the consensus fault threshold). This is within the stated scope. The attack itself is trivial to execute once network access is established: simply open many TCP connections and send no data.

### Recommendation

Apply a read timeout to the raw TCP stream before the TLS handshake in `start_server`, mirroring the pattern used in the public HTTP endpoint:

```rust
let mut stream = tokio_io_timeout::TimeoutStream::new(stream);
stream.set_read_timeout(Some(read_timeout));
let stream = Box::pin(stream);
match tls_acceptor.accept(stream).await { ... }
```

Alternatively, wrap `tls_acceptor.accept(stream)` with `tokio::time::timeout(read_timeout, ...)` to bound the TLS handshake duration. A configurable `connection_read_timeout_seconds` field should be added to the XNet config (analogous to `rs/config/src/http_handler.rs`).

### Proof of Concept

1. Register a node in the IC registry (or control an existing registered node).
2. From that node's IP, open thousands of TCP connections to the target replica's XNet port (default configured via `xnet_port`).
3. For each connection, send no bytes after the TCP handshake.
4. Each connection spawns a Tokio task blocked on `tls_acceptor.accept(stream).await` with no timeout.
5. After exhausting file descriptors or Tokio task memory, the target node's XNet endpoint stops accepting new legitimate connections, halting cross-subnet stream slice delivery.

---

**Root cause location:** [1](#0-0) 

**No timeout on TLS accept:** [2](#0-1) 

**Contrast — public endpoint correctly applies read timeout before TLS:** [3](#0-2) 

**Semaphore only limits requests, not connections:** [4](#0-3) 

**Metrics endpoint also lacks connection-level read timeout:** [5](#0-4)

### Citations

**File:** rs/http_endpoints/xnet/src/lib.rs (L54-54)
```rust
const XNET_ENDPOINT_MAX_CONCURRENT_REQUESTS: usize = 4;
```

**File:** rs/http_endpoints/xnet/src/lib.rs (L240-293)
```rust
                    tokio::spawn(async move {
                        let http = hyper_util::server::conn::auto::Builder::new(hyper_util::rt::TokioExecutor::new());

                        #[cfg(test)]
                        {
                            // TLS is not used in tests.
                            let _ = tls;
                            let _ = registry_client;

                            let io = TokioIo::new(stream);
                            let conn = http.serve_connection(io, hyper_service);
                            if let Err(err) = conn.await {
                                warn!(logger, "failed to serve connection: {err}");
                            }
                        }

                        #[cfg(not(test))]
                        {
                            // Creates a new TLS server config and uses it to accept the request.
                            let registry_version = registry_client.get_latest_version();
                            let mut server_config = match tls.server_config(
                                ic_crypto_tls_interfaces::SomeOrAllNodes::All,
                                registry_version,
                            ) {
                                Ok(config) => config,
                                Err(err) => {
                                    warn!(logger, "Failed to get server config from crypto {err}");
                                    return;
                                }
                            };

                            const ALPN_HTTP2: &[u8; 2] = b"h2";
                            const ALPN_HTTP1_1: &[u8; 8] = b"http/1.1";
                            server_config.alpn_protocols = vec![ALPN_HTTP2.to_vec(), ALPN_HTTP1_1.to_vec()];

                            let tls_acceptor =
                                tokio_rustls::TlsAcceptor::from(Arc::new(server_config));
                            match tls_acceptor.accept(stream).await {
                                Ok(tls_stream) => {
                                    let io = TokioIo::new(tls_stream);
                                    let conn = http.serve_connection(io, hyper_service);
                                    if let Err(err) = conn.await {
                                        warn!(logger, "failed to serve connection: {err}");
                                        metrics.closed_connections_total.inc();
                                    }
                                }
                                Err(err) => {
                                    warn!(logger, "Error setting up TLS stream: {err}");
                                    metrics.closed_connections_total.inc();

                                }
                            };
                        }
                    });
```

**File:** rs/http_endpoints/public/src/lib.rs (L473-494)
```rust
                match timeout(read_timeout, stream.peek(&mut b)).await {
                    Ok(Ok(_)) => {}
                    Ok(Err(_)) => {
                        metrics
                            .connection_setup_duration
                            .with_label_values(&[STATUS_ERROR, LABEL_IO_ERROR])
                            .observe(timer.elapsed().as_secs_f64());
                        metrics.closed_connections_total.inc();
                        return;
                    }
                    Err(_) => {
                        metrics
                            .connection_setup_duration
                            .with_label_values(&[STATUS_ERROR, LABEL_TIMEOUT_ERROR])
                            .observe(timer.elapsed().as_secs_f64());
                        metrics.closed_connections_total.inc();
                        return;
                    }
                }
                let mut stream = tokio_io_timeout::TimeoutStream::new(stream);
                stream.set_read_timeout(Some(read_timeout));
                let stream = Box::pin(stream);
```

**File:** rs/http_endpoints/metrics/src/lib.rs (L142-146)
```rust
        self.rt_handle.spawn(async move {
            axum::serve(tcp_listener, metrics_service)
                .await
                .expect("Failed to serve.")
        });
```
