### Title
Unbounded `inbound_connecting` JoinSet Allows Pre-Authentication Resource Exhaustion via QUIC Connection Flood — (`rs/p2p/quic_transport/src/connection_manager.rs`)

---

### Summary

The QUIC transport connection manager spawns an unbounded number of Tokio tasks into `inbound_connecting` for every accepted `Incoming` QUIC connection, before any TLS or subnet-membership authentication occurs. An off-subnet attacker who can send UDP packets to the replica's transport port can flood the endpoint with QUIC Initial packets, causing the JoinSet to grow without limit and exhausting heap memory or Tokio task scheduler capacity, starving legitimate subnet peer connections.

---

### Finding Description

`inbound_connecting` is declared as a plain, unbounded `JoinSet`: [1](#0-0) 

The event loop's `select!` branch accepts every `Incoming` from `endpoint.accept()` unconditionally and immediately calls `handle_inbound_conn_attemp`: [2](#0-1) 

`handle_inbound_conn_attemp` spawns a task into `inbound_connecting` for every `Incoming` with no size check: [3](#0-2) 

All authentication — TLS handshake completion (`incoming.await`), certificate extraction, `node_id_from_certificate_der`, and the `peer_id > node_id` dialer check — happens **inside** the spawned task, after the task is already in the JoinSet: [4](#0-3) 

The TLS server config is updated on topology change to restrict to subnet nodes, but this restriction is enforced during the TLS handshake (i.e., inside the spawned task when `incoming.await` is polled), not before the task is spawned: [5](#0-4) 

In Quinn's architecture, `endpoint.accept()` yields an `Incoming` after QUIC address validation (retry token exchange) but **before** the TLS handshake. An attacker with a real IP address can complete address validation by responding to Quinn's Retry packet, causing `Incoming` objects to be yielded and tasks to be spawned, even without a valid subnet TLS certificate.

Each spawned task holds a pending QUIC connection state and lives for up to `CONNECT_TIMEOUT = 10s` before timing out: [6](#0-5) 

A search across the entire `quic_transport` module confirms there is no `max_incoming`, `concurrent_connections`, connection limit, or `inbound_connecting.len()` guard anywhere: [7](#0-6) 

---

### Impact Explanation

An attacker maintaining a sustained flood of QUIC Initial packets (each completing address validation) can accumulate thousands of tasks in `inbound_connecting`. Each task holds heap-allocated QUIC connection state for up to 10 seconds. At sufficient rate, this exhausts heap memory or saturates the Tokio task scheduler, preventing the connection manager's `select!` loop from processing legitimate inbound connections from subnet peers. This directly stalls consensus peer connectivity, which can halt block finalization on the affected replica.

---

### Likelihood Explanation

The replica's QUIC transport port is publicly reachable (required for P2P subnet communication). The attacker only needs to send UDP packets and respond to Quinn's Retry packets (address validation), which requires no cryptographic material. No privileged access, key compromise, or majority corruption is needed. The attack is sustainable: new tasks are spawned faster than they time out at 10s, so a modest packet rate maintains a large JoinSet indefinitely.

---

### Recommendation

1. **Cap `inbound_connecting`**: Before calling `self.inbound_connecting.spawn(...)`, check `self.inbound_connecting.len()` against a hard limit (e.g., `2 × subnet_size`). If the limit is exceeded, call `incoming.refuse()` or drop the `Incoming` to reject the connection before spawning.
2. **Use `Incoming::refuse()`**: Quinn's `Incoming` type exposes a `refuse()` method that sends a QUIC `CONNECTION_CLOSE` frame without completing the handshake, cleanly rejecting the connection at zero task cost.
3. **Per-IP rate limiting**: Track connection attempts per source IP in the event loop (before spawning) and drop `Incoming` objects from IPs exceeding a threshold.

---

### Proof of Concept

```
1. Attacker opens N UDP sockets to replica's transport port.
2. For each socket, send a QUIC Initial packet (valid ClientHello, any or no cert).
3. Respond to Quinn's Retry packet with the correct token to pass address validation.
4. endpoint.accept() yields Incoming → handle_inbound_conn_attemp spawns task.
5. Repeat at rate R > (JoinSet_drain_rate = tasks_completing_per_second).
6. inbound_connecting.len() grows without bound.
7. At ~10,000 tasks: heap pressure causes OOM or Tokio scheduler starvation.
8. Legitimate subnet peer Incoming objects are accepted but their tasks never get
   scheduled → CONNECT_TIMEOUT fires → peers retry → consensus stalls.
```

The invariant `inbound_connecting` must be bounded and pre-authentication resource consumption must be O(1) per attempt is violated: there is no bound, and each attempt consumes O(heap) for up to 10 seconds before any authentication check runs.

### Citations

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L82-82)
```rust
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L119-121)
```rust
    /// Task joinset on which incoming connection requests are spawned. This is not a JoinMap
    /// because the peerId is not available until the TLS handshake succeeded.
    inbound_connecting: JoinSet<Result<ConnectionWithPeerId, ConnectionEstablishError>>,
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L217-231)
```rust
    let mut transport_config = quinn::TransportConfig::default();

    transport_config
        .max_idle_timeout(Some(IDLE_TIMEOUT.try_into().unwrap()))
        .keep_alive_interval(Some(KEEP_ALIVE_INTERVAL))
        .send_window(SEND_WINDOW)
        .receive_window(RECEIVE_WINDOW)
        .stream_receive_window(STREAM_RECEIVE_WINDOW)
        .max_concurrent_bidi_streams(MAX_CONCURRENT_BIDI_STREAMS)
        .max_concurrent_uni_streams(MAX_CONCURRENT_UNI_STREAMS);

    let transport_config = Arc::new(transport_config);
    let quinn_server_config = QuicServerConfig::try_from(rustls_server_config).expect("Conversion from RustTls config to Quinn config must succeed as long as this library and quinn use the same RustTls versions.");
    let mut server_config = quinn::ServerConfig::with_crypto(Arc::new(quinn_server_config));
    server_config.transport_config(transport_config.clone());
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L354-362)
```rust
                incoming = self.endpoint.accept() => {
                    if let Some(incoming) = incoming {
                        self.handle_inbound_conn_attemp(incoming);
                    } else {
                        error!(self.log, "Quic endpoint closed. Stopping transport.");
                        // Endpoint is closed. This indicates NOT graceful shutdown.
                        break;
                    }
                },
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L403-411)
```rust
        // Set new server config to only accept connections from the current set.
        let rustls_server_config = self.tls_config
            .server_config(subnet_nodes, self.topology.latest_registry_version())
            .expect("The rustls server config must be locally available, otherwise transport can't run.");

        let quic_server_config = QuicServerConfig::try_from(rustls_server_config).expect("Conversion from RustTls config to Quinn config must succeed as long as this library and quinn use the same RustTls versions.");
        let mut server_config = quinn::ServerConfig::with_crypto(Arc::new(quic_server_config));
        server_config.transport_config(self.transport_config.clone());
        self.endpoint.set_server_config(Some(server_config));
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L538-588)
```rust
    fn handle_inbound_conn_attemp(&mut self, incoming: Incoming) {
        self.metrics.inbound_connection_total.inc();
        let node_id = self.node_id;
        let conn_fut = async move {
            let established =
                incoming
                    .await
                    .map_err(|cause| ConnectionEstablishError::ConnectionError {
                        peer_id: None,
                        cause,
                    })?;

            let rustls_certs = established
                .peer_identity()
                .ok_or(ConnectionEstablishError::AuthenticationFailed(
                    "missing peer identity".to_string(),
                ))?
                .downcast::<Vec<CertificateDer>>()
                .unwrap();
            let rustls_cert =
                rustls_certs
                    .first()
                    .ok_or(ConnectionEstablishError::AuthenticationFailed(
                        "a single cert must be present".to_string(),
                    ))?;
            let peer_id = node_id_from_certificate_der(rustls_cert.as_ref())
                .map_err(|err| ConnectionEstablishError::AuthenticationFailed(err.to_string()))?;

            // Lower ID is dialer. So we reject if this nodes id is higher.
            if peer_id > node_id {
                return Err(ConnectionEstablishError::InvalidIncomingPeerId {
                    client: peer_id,
                    server: node_id,
                });
            }

            Ok::<_, ConnectionEstablishError>(ConnectionWithPeerId {
                peer_id,
                connection: established,
            })
        };

        let timeout_conn_fut = async move {
            match tokio::time::timeout(CONNECT_TIMEOUT, conn_fut).await {
                Ok(connection_res) => connection_res,
                Err(_) => Err(ConnectionEstablishError::Timeout),
            }
        };

        self.inbound_connecting.spawn(timeout_conn_fut);
    }
```
