### Title
Unbounded `inbound_connecting` JoinSet Allows Single Byzantine Subnet Peer to Exhaust Replica Memory via QUIC Connection Flooding — (`rs/p2p/quic_transport/src/connection_manager.rs`)

---

### Summary

`handle_inbound_conn_attemp` unconditionally spawns a Tokio task into `inbound_connecting` (a plain, unbounded `JoinSet`) for every accepted `Incoming` QUIC connection, with no per-peer cap, no global cap, and no `concurrent_connections` limit on the Quinn `ServerConfig`. Because TLS authentication occurs *inside* the spawned task (after `incoming.await`), a single Byzantine subnet peer holding a valid TLS certificate can flood the JoinSet by rapidly initiating QUIC handshakes, causing unbounded task accumulation, memory exhaustion, and Tokio scheduler degradation on the target replica.

---

### Finding Description

**Entrypoint — `ConnectionManager::run` (line 354–361):**

Every time `self.endpoint.accept()` yields an `Incoming`, the event loop immediately calls `handle_inbound_conn_attemp(incoming)` with no pre-check. [1](#0-0) 

**Root cause — `handle_inbound_conn_attemp` (line 538–588):**

The function unconditionally spawns a new task into `self.inbound_connecting` at line 587. There is no check on the current size of the JoinSet, no per-peer counter, and no rate limit. [2](#0-1) 

**Authentication is deferred inside the task (lines 541–577):**

The TLS handshake (`incoming.await`) and certificate extraction happen *inside* the spawned future. The task is already in the JoinSet before any identity check occurs. [3](#0-2) 

**`inbound_connecting` is a plain, unbounded `JoinSet` (line 121):**

Unlike `outbound_connecting`, which is a `JoinMap<NodeId, …>` (at most one task per peer), `inbound_connecting` has no keying, no deduplication, and no capacity bound. [4](#0-3) 

**`CONNECT_TIMEOUT` is 10 seconds (line 82):**

Each spawned task lives for up to 10 seconds before timing out. An attacker sending N connections per second accumulates up to 10×N live tasks simultaneously. [5](#0-4) 

**No `concurrent_connections` limit on the Quinn `ServerConfig`:**

The server config construction (lines 229–231) never calls `.concurrent_connections(…)`, leaving Quinn's internal default (effectively unlimited) in place. The grep for `concurrent_connections` across the entire `quic_transport` crate returns zero matches. [6](#0-5) 

---

### Impact Explanation

A Byzantine subnet peer can drive `inbound_connecting.len()` to an arbitrary value. Each entry is a live Tokio task holding an `Incoming` handle and associated QUIC state. At scale this causes:

- **Memory exhaustion**: heap grows proportionally to the number of live tasks and their QUIC buffers.
- **Tokio scheduler degradation**: the scheduler must poll thousands of pending tasks per tick, starving legitimate work (consensus, artifact pool, state sync).
- **Replica unavailability**: the target replica drops out of consensus, stalling the subnet.

The `connecting_connections` metric (line 374–375) will reflect the inflated count but provides no enforcement. [7](#0-6) 

---

### Likelihood Explanation

The precondition is a single Byzantine subnet node with a valid TLS certificate — a standard Byzantine fault assumption for IC (threshold f < n/3). No majority corruption, no key leakage, and no external infrastructure attack is required. The attack is purely protocol-level: send QUIC Initial packets as fast as the network allows. QUIC Initial packets are ~1200 bytes; a single node can sustain thousands per second on a LAN or datacenter link.

---

### Recommendation

1. **Cap `inbound_connecting` globally**: before spawning, check `self.inbound_connecting.len()` against a constant (e.g., `2 × subnet_size`) and call `incoming.refuse()` if the cap is exceeded.
2. **Per-peer cap before TLS**: use Quinn's `Incoming::remote_address()` to extract the source IP before spawning, and reject if a per-IP counter exceeds a threshold.
3. **Set `ServerConfig::concurrent_connections`**: call `.concurrent_connections(MAX)` on the Quinn `ServerConfig` so Quinn itself drops excess `Incoming` objects before they reach the application layer.
4. **Replace `JoinSet` with a bounded `JoinMap`**: once the peer identity is known (post-handshake), the existing `InvalidIncomingPeerId` check already rejects wrong-direction connections; a `JoinMap` keyed by source address would also prevent duplicate in-flight tasks from the same peer.

---

### Proof of Concept

```rust
// Pseudocode fuzz harness
let byzantine_endpoint = make_endpoint_with_valid_subnet_cert();
let target_addr = replica_quic_addr();

// Spawn N concurrent QUIC Initial packets without completing handshakes
let mut handles = vec![];
for _ in 0..10_000 {
    let ep = byzantine_endpoint.clone();
    handles.push(tokio::spawn(async move {
        let connecting = ep.connect(target_addr, "irrelevant").unwrap();
        // Drop immediately — never complete the handshake
        drop(connecting);
    }));
}
join_all(handles).await;

// Assert: target replica's `connecting_connections` metric is NOT capped
// Expected (vulnerable): metric == 10_000
// Expected (fixed):       metric <= MAX_INBOUND_CAP
let metric = scrape_metric(target_addr, "quic_transport_connecting_connections");
assert!(metric <= MAX_INBOUND_CAP, "JoinSet unbounded: got {}", metric);
```

### Citations

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L82-82)
```rust
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L119-122)
```rust
    /// Task joinset on which incoming connection requests are spawned. This is not a JoinMap
    /// because the peerId is not available until the TLS handshake succeeded.
    inbound_connecting: JoinSet<Result<ConnectionWithPeerId, ConnectionEstablishError>>,
    /// JoinMap that stores active connection handlers keyed by peer id.
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L229-231)
```rust
    let quinn_server_config = QuicServerConfig::try_from(rustls_server_config).expect("Conversion from RustTls config to Quinn config must succeed as long as this library and quinn use the same RustTls versions.");
    let mut server_config = quinn::ServerConfig::with_crypto(Arc::new(quinn_server_config));
    server_config.transport_config(transport_config.clone());
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L354-361)
```rust
                incoming = self.endpoint.accept() => {
                    if let Some(incoming) = incoming {
                        self.handle_inbound_conn_attemp(incoming);
                    } else {
                        error!(self.log, "Quic endpoint closed. Stopping transport.");
                        // Endpoint is closed. This indicates NOT graceful shutdown.
                        break;
                    }
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L373-376)
```rust
            self.metrics
                .connecting_connections
                .set(self.inbound_connecting.len() as i64 + self.outbound_connecting.len() as i64);
            self.metrics
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L538-587)
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
```
