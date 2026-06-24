Let me check the TLS configuration details and whether Quinn provides any pre-handshake rejection mechanism.

### Title
Unbounded `inbound_connecting` JoinSet Allows Pre-Authentication Resource Exhaustion via QUIC Connection Flood — (`rs/p2p/quic_transport/src/connection_manager.rs`)

---

### Summary

The `ConnectionManager` spawns a Tokio task into an unbounded `JoinSet` for every accepted QUIC `Incoming` connection before any authentication check occurs. TLS/mTLS verification only runs inside the spawned task. An off-subnet attacker with a real IP reachable to the replica's QUIC UDP port can flood the endpoint with QUIC Initial packets, causing unbounded task accumulation, CPU exhaustion from concurrent TLS handshakes, and starvation of legitimate subnet peer connections.

---

### Finding Description

**Unbounded JoinSet declaration:**

`inbound_connecting` is declared as a plain `JoinSet` with no capacity limit: [1](#0-0) 

It is initialized with `JoinSet::new()` and no bound is ever checked: [2](#0-1) 

**Unconditional task spawn on every `Incoming`:**

The event loop accepts every incoming connection and immediately calls `handle_inbound_conn_attemp` with no pre-check: [3](#0-2) 

Inside `handle_inbound_conn_attemp`, a task is spawned unconditionally: [4](#0-3) 

**TLS authentication happens inside the spawned task:**

The `incoming.await` call — which drives the QUIC+TLS handshake and invokes `NodeClientCertVerifier` — is inside the spawned async block, not before it: [5](#0-4) 

The `NodeClientCertVerifier` with `SomeOrAllNodes::Some(subnet_node_set)` enforces that the client certificate's `NodeId` is in the allowed set and matches the registry. But this runs during the TLS handshake, which is inside the task: [6](#0-5) 

The server config restricting to subnet nodes is set on topology change, but this only affects which TLS handshakes succeed — it does not prevent `Incoming` objects from being created and tasks from being spawned: [7](#0-6) 

**No `incoming.refuse()`, no rate limit, no JoinSet size cap anywhere in the transport code.** Searches for `incoming.refuse`, `max_incoming`, `connection_limit`, and `inbound_connecting.len` return zero matches.

**CONNECT_TIMEOUT = 10 seconds**, meaning each attacker-spawned task holds resources for up to 10 seconds before timing out: [8](#0-7) 

---

### Impact Explanation

Each spawned task holds:
- A Tokio task (stack + heap allocation)
- A QUIC connection state machine
- A pending TLS 1.3 handshake with mandatory Ed25519 client authentication

The server performs its own Ed25519 signing and sends `ServerHello`, `Certificate`, `CertificateVerify`, and `Finished` before it can verify the client certificate. This is non-trivial CPU work per connection. With thousands of concurrent tasks each performing TLS handshakes, the Tokio worker thread pool saturates. Legitimate subnet peer connections queued in `inbound_connecting` or `outbound_connecting` are starved, breaking P2P connectivity and stalling consensus finalization.

---

### Likelihood Explanation

The attacker only needs:
1. A real IP address reachable to the replica's QUIC UDP port (QUIC address validation via Retry prevents IP spoofing, but not real-IP floods)
2. The ability to send QUIC Initial packets — standard UDP traffic

No subnet membership, no valid certificate, no privileged access is required. The replica's transport port is necessarily reachable to subnet peers, and therefore reachable to any attacker on the same network path. A few hundred concurrent QUIC connections sustained over 10-second windows is sufficient to exhaust CPU given the Ed25519 handshake cost.

---

### Recommendation

1. **Cap `inbound_connecting`**: Before calling `self.inbound_connecting.spawn(...)`, check `self.inbound_connecting.len()` against a maximum (e.g., `2 × topology_size` or a fixed constant like 100). If the cap is reached, call `incoming.refuse()` to reject the connection at the QUIC layer before any TLS work begins.

2. **Pre-handshake IP allowlisting**: Optionally, check the remote address of `incoming` against known subnet peer addresses before spawning, using `incoming.remote_address()`.

3. **Use `incoming.refuse()`**: Quinn exposes `Incoming::refuse()` to reject a connection before the TLS handshake, consuming minimal server resources.

---

### Proof of Concept

```
1. Attacker opens N concurrent UDP sockets and sends QUIC Initial packets
   to the replica's transport port (completing QUIC address validation).
2. Each packet causes endpoint.accept() → handle_inbound_conn_attemp →
   inbound_connecting.spawn(timeout_conn_fut).
3. Each task calls incoming.await, triggering a TLS 1.3 handshake.
   Server performs Ed25519 signing before client cert verification.
4. After ~100-500 concurrent tasks, Tokio worker threads are saturated
   with TLS crypto work.
5. Legitimate subnet peer connections (outbound_connecting or inbound
   from real peers) cannot be processed; select! branches for
   outbound_connecting and active_connections are starved.
6. Consensus peer connectivity drops → finalization stalls.
```

A turmoil simulation test can confirm this: flood `N` inbound connections from non-subnet IPs and assert that a legitimate subnet peer connection still completes within `CONNECT_TIMEOUT`.

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

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L256-256)
```rust
        inbound_connecting: JoinSet::new(),
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

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L401-411)
```rust
        let subnet_nodes = SomeOrAllNodes::Some(subnet_node_set);

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

**File:** rs/crypto/src/tls/rustls/node_cert_verifier.rs (L218-252)
```rust
fn verify_node_cert(
    end_entity_der: &CertificateDer,
    intermediates: &[CertificateDer],
    allowed_nodes: &SomeOrAllNodes,
    registry_client: &dyn RegistryClient,
    registry_version: RegistryVersion,
    current_time: Time,
) -> Result<(), TLSError> {
    ensure_intermediate_certs_empty(intermediates)?;
    let end_entity_node_id =
        node_id_from_certificate_der(end_entity_der.as_ref()).map_err(|err| match err {
            NodeIdFromCertificateDerError::InvalidCertificate(_) => {
                TLSError::InvalidCertificate(CertificateError::BadEncoding)
            }
            NodeIdFromCertificateDerError::UnexpectedContent(e) => TLSError::InvalidCertificate(
                CertificateError::Other(OtherError(Arc::from(Box::from(e)))),
            ),
        })?;

    ensure_node_id_in_allowed_nodes(end_entity_node_id, allowed_nodes)?;
    let node_cert_from_registry =
        node_cert_from_registry(end_entity_node_id, registry_client, registry_version)?;
    ensure_certificates_equal(
        end_entity_der.as_ref(),
        end_entity_node_id,
        node_cert_from_registry.as_der(),
    )?;
    // It's important to do the validity check after checking equality to the
    // registry cert because the cert validation uses a different parser
    // (`x509_parser` as opposed to OpenSSL that is used above) and it is safer
    // to not just pass any untrusted data to it. We consider the DER here trusted
    // because it is equal to the certificate DER stored in the registry, as checked
    // above.
    ensure_node_certificate_is_valid(end_entity_der.to_vec(), end_entity_node_id, current_time)?;
    Ok(())
```
