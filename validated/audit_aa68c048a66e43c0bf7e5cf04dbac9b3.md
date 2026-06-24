Audit Report

## Title
Unbounded `inbound_connecting` JoinSet Enables Byzantine Subnet Peer to Exhaust Replica Resources via QUIC Connection Flood — (`rs/p2p/quic_transport/src/connection_manager.rs`)

## Summary
`handle_inbound_conn_attemp` unconditionally spawns a Tokio task into the unbounded `inbound_connecting` JoinSet for every accepted QUIC `Incoming`, with no per-peer cap, no global cap, and no deduplication. A Byzantine subnet peer holding a valid NNS-issued TLS certificate can open arbitrarily many concurrent QUIC connections, causing unbounded task accumulation (each alive up to `CONNECT_TIMEOUT` = 10 s), exhausting Tokio task heap and QUIC connection state and potentially OOM-crashing the replica.

## Finding Description

**Root cause — `handle_inbound_conn_attemp` (L538–588):**
Every accepted `Incoming` is unconditionally wrapped in a timeout future and spawned with no guard:

```rust
// L580-587
let timeout_conn_fut = async move {
    match tokio::time::timeout(CONNECT_TIMEOUT, conn_fut).await {
        Ok(connection_res) => connection_res,
        Err(_) => Err(ConnectionEstablishError::Timeout),
    }
};
self.inbound_connecting.spawn(timeout_conn_fut);
```

The backing store is a plain, unbounded `JoinSet` (L121):
```rust
inbound_connecting: JoinSet<Result<ConnectionWithPeerId, ConnectionEstablishError>>,
```

**Contrast with outbound path (L118):**
```rust
outbound_connecting: JoinMap<NodeId, Result<Connection, ConnectionEstablishError>>,
```
`outbound_connecting` is a `JoinMap` keyed by `NodeId`, enforcing at most one outbound task per peer. `inbound_connecting` has no such key, no deduplication, and no size bound.

**TLS guard is insufficient (L401–411):**
The server config is updated to only allow current subnet members. This blocks non-subnet nodes from completing TLS. However, a Byzantine peer *is* a subnet member with a valid certificate — its connections complete TLS successfully, so every spawned task runs to completion. The TLS guard filters outsiders only, not misbehaving insiders.

**Event loop drains one completion per iteration (L292–368):**
The `select!` loop handles exactly one branch per iteration. If the attacker opens connections faster than `inbound_connecting.join_next()` is polled, the JoinSet grows without bound.

**`CONNECT_TIMEOUT` = 10 s (L82):**
Each task lives up to 10 seconds, giving the attacker a sustained accumulation window.

## Impact Explanation

Each spawned task holds a Tokio task stack allocation, a `quinn::Incoming`/`quinn::Connection` object (QUIC connection state, crypto context, stream buffers), and a reference to the endpoint's internal connection table entry. With N concurrent connections from one Byzantine peer, the replica accumulates N tasks × (task overhead + QUIC connection state). At scale this exhausts Tokio task heap memory or OS-level resources (file descriptors, UDP socket receive-buffer entries), stalling or OOM-crashing the replica's event loop.

This maps to the **High** bounty impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."* This is not raw volumetric DDoS — it is an authenticated, protocol-level resource exhaustion attack exploiting a specific unbounded data structure. A crashed replica loses its role in consensus; if enough replicas are targeted simultaneously, subnet availability and consensus liveness are at risk.

## Likelihood Explanation

The attacker is a Byzantine subnet peer — explicitly within the IC's threat model (up to `f` Byzantine nodes in a subnet of `3f+1`). The peer holds a valid NNS-issued node TLS certificate. Opening many concurrent QUIC connections from a single process is trivial using the `quinn` client library in a loop. No privileged access, no key compromise, and no majority corruption is required. The attack is locally reproducible and repeatable.

## Recommendation

1. **Add a global cap** on `inbound_connecting.len()` (e.g., `2 × subnet_size`) before spawning; call `incoming.refuse()` when the cap is reached.
2. **Add a per-peer inbound connection cap** by tracking in-flight inbound tasks per source IP/`NodeId` and dropping `Incoming` objects that exceed the limit (e.g., 2–3 concurrent attempts per peer).
3. **Consider migrating `inbound_connecting` to a `JoinMap`** keyed by source IP or peer identity once identity is extractable pre-handshake, mirroring the deduplication already present in `outbound_connecting`.

## Proof of Concept

```rust
// Byzantine peer opens N concurrent QUIC connections to victim replica
let client_config = make_client_config_with_valid_node_cert();
let mut handles = Vec::new();
for _ in 0..N {
    let cfg = client_config.clone();
    handles.push(tokio::spawn(async move {
        let endpoint = quinn::Endpoint::client("0.0.0.0:0".parse().unwrap()).unwrap();
        let _ = endpoint
            .connect_with(cfg, victim_addr, "irrelevant")
            .unwrap()
            .await; // completes TLS; task stays in inbound_connecting for up to 10s
    }));
}
// Assert: victim's inbound_connecting.len() grows to N
// Assert: victim RSS grows proportionally to N
// Assert: victim event loop latency degrades as N increases
```

A deterministic integration test can instantiate a local `ConnectionManager`, connect N times from a test peer with a valid cert, and assert `inbound_connecting.len() == N` and that the event loop stalls.