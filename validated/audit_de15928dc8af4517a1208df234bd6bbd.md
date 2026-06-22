### Title
Unbounded Inbound Connection Task Accumulation Enables Resource Exhaustion DoS - (File: rs/p2p/quic_transport/src/connection_manager.rs)

### Summary
The QUIC transport layer's `ConnectionManager` spawns an async task into an unbounded `JoinSet` for every incoming QUIC connection attempt, before any application-level rate limiting or size cap is applied. A malicious subnet peer can flood a victim node with repeated QUIC connection initiations, causing unbounded task accumulation in `inbound_connecting`, each task performing a TLS handshake (a crypto-intensive operation), exhausting CPU and memory on the victim node.

### Finding Description

In `rs/p2p/quic_transport/src/connection_manager.rs`, the `ConnectionManager` struct holds:

```rust
inbound_connecting: JoinSet<Result<ConnectionWithPeerId, ConnectionEstablishError>>,
``` [1](#0-0) 

This `JoinSet` is initialized with no capacity bound: [2](#0-1) 

Every incoming QUIC connection is accepted from the event loop and unconditionally dispatched to `handle_inbound_conn_attemp`: [3](#0-2) 

Inside `handle_inbound_conn_attemp`, a new async task is spawned into `inbound_connecting` with **no check on the current size of the set**: [4](#0-3) 

The TLS handshake (including certificate verification and `node_id_from_certificate_der`) executes **inside** the spawned task, not before it. This means each connection attempt consumes a task slot and triggers a crypto operation before any rejection can occur. [5](#0-4) 

By contrast, the outbound side uses a `JoinMap<NodeId, ...>` which is naturally bounded by the number of unique peer IDs (subnet size). The inbound side has no equivalent bound. [6](#0-5) 

Each spawned task lives for up to `CONNECT_TIMEOUT` (10 seconds): [7](#0-6) 

A malicious peer sending N connection attempts per second can maintain N×10 concurrent tasks in `inbound_connecting`, each holding a QUIC connection object and performing TLS crypto work.

### Impact Explanation

A single malicious subnet node (a protocol peer below the consensus fault threshold) can continuously initiate QUIC connections to a victim replica node. Each initiation spawns an unbounded async task performing TLS handshake crypto. Accumulating thousands of such tasks exhausts the Tokio runtime's CPU and heap memory on the victim node, causing it to become unresponsive to legitimate subnet peers, degrading or halting consensus participation. This is a resource accounting bug in the transport layer directly analogous to the reported peer-list flooding issue.

### Likelihood Explanation

The QUIC transport port (UDP 4100) is restricted by nftables firewall rules to whitelisted node IPs: [8](#0-7) 

This means the attacker must be a node whose IP is whitelisted — i.e., a current or recently-removed subnet member. A single malicious subnet node (below the 1/3 fault threshold needed to break consensus) can execute this attack. Given that subnets can have tens of nodes and node operators are permissioned but not individually trusted, this is a realistic threat. The attack requires no special privileges beyond subnet membership.

### Recommendation

1. **Cap `inbound_connecting` size**: Before calling `self.inbound_connecting.spawn(...)`, check `self.inbound_connecting.len()` against a hard limit (e.g., `2 × subnet_size`). If the limit is exceeded, reject the incoming connection immediately by calling `incoming.refuse()` or dropping it.
2. **Per-peer rate limiting**: Track inbound connection attempts per source `NodeId` and reject excessive reconnects within a short window.
3. **Reject before spawning**: Perform a lightweight pre-check (e.g., source IP allowlist check at the QUIC layer) before spawning the TLS handshake task, analogous to the recommendation in the original report to limit the peer list before performing signature checks.

### Proof of Concept

A malicious node M in the subnet repeatedly calls `quinn::Endpoint::connect()` targeting victim node V's UDP port 4100, without completing the handshake (or completing it and immediately reconnecting). Each attempt causes V's `ConnectionManager` to execute:

```
incoming = self.endpoint.accept() => {
    self.handle_inbound_conn_attemp(incoming);  // spawns task, no size check
}
```

After sending K attempts within 10 seconds, V holds K concurrent tasks in `inbound_connecting`, each running TLS handshake crypto. At a rate of 1000 attempts/second, V accumulates 10,000 concurrent tasks, exhausting its Tokio runtime and causing it to miss consensus deadlines. [3](#0-2) [9](#0-8)

### Citations

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L82-83)
```rust
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const CONNECT_RETRY_BACKOFF: Duration = Duration::from_secs(5);
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L117-123)
```rust
    /// Task joinmap that holds stores a connecting tasks keys by peer id.
    outbound_connecting: JoinMap<NodeId, Result<Connection, ConnectionEstablishError>>,
    /// Task joinset on which incoming connection requests are spawned. This is not a JoinMap
    /// because the peerId is not available until the TLS handshake succeeded.
    inbound_connecting: JoinSet<Result<ConnectionWithPeerId, ConnectionEstablishError>>,
    /// JoinMap that stores active connection handlers keyed by peer id.
    active_connections: JoinMap<NodeId, ()>,
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L255-257)
```rust
        outbound_connecting: JoinMap::new(),
        inbound_connecting: JoinSet::new(),
        active_connections: JoinMap::new(),
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

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L537-588)
```rust
    /// Inserts a task into 'inbound_connecting' that handles an inbound connection attempt.
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

**File:** rs/orchestrator/testdata/nftables_assigned_replica.conf.golden (L41-41)
```text
    ip saddr {1.1.1.1,3.0.0.3,3.0.0.5,3.0.0.6,4.0.0.4,4.0.0.6,4.0.0.7} udp dport {4100} accept # Automatic whitelisted nodes whitelisting
```
