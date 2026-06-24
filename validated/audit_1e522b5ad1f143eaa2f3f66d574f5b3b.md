Looking at the actual code to verify each claim before rendering a verdict.

Audit Report

## Title
Unbounded `inbound_connecting` JoinSet Allows Byzantine Subnet Peer to Exhaust Replica Resources via QUIC Handshake Stalling — (`rs/p2p/quic_transport/src/connection_manager.rs`)

## Summary
`handle_inbound_conn_attemp` unconditionally spawns a Tokio task into `inbound_connecting` for every `Incoming` object surfaced by `endpoint.accept()`, with no cap on the JoinSet size, no per-peer rate limit, and no Quinn-level `concurrent_connections` bound. A single Byzantine subnet node holding a valid TLS certificate can initiate thousands of QUIC connections and stall each at the TLS handshake phase (before sending the client Certificate/Finished), causing the JoinSet to accumulate an unbounded number of live tasks for up to `CONNECT_TIMEOUT` (10 s) each, exhausting Tokio task memory and CPU on the victim replica.

## Finding Description
`handle_inbound_conn_attemp` (L538–588) is called synchronously from the `run` `select!` loop (L354–357) every time `endpoint.accept()` yields an `Incoming`. It unconditionally calls `self.inbound_connecting.spawn(timeout_conn_fut)` (L587) with no prior check of `self.inbound_connecting.len()`. The `inbound_connecting` field is declared as a plain `JoinSet::new()` (L121, L256) with no capacity bound.

The only authentication gate — `NodeClientCertVerifier::verify_client_cert` — is embedded in the rustls TLS stack and is only invoked when the remote peer sends its TLS `Certificate` message during the handshake. This call happens inside the spawned task when `incoming.await` (L544) resolves. An attacker who sends the QUIC Initial / ClientHello but never sends the TLS `Certificate`+`Finished` messages stalls the server-side TLS state machine indefinitely, keeping the task alive until `CONNECT_TIMEOUT` fires (L581–584). The `IDLE_TIMEOUT` of 5 s applies only to established QUIC connections, not to connections in the handshake phase.

The `quinn::ServerConfig` is constructed with only `transport_config` set (L230–231); `concurrent_connections` is never called, so Quinn's internal limit defaults to `u32::MAX`. `EndpointConfig::default()` (L207) likewise sets no `max_incoming` bound. No rate-limiting or per-source-IP guard exists anywhere in `rs/p2p/quic_transport/`.

## Impact Explanation
A sustained flood of stalled handshakes produces a steady-state JoinSet of thousands of live Tokio tasks, each holding QUIC connection state and a TLS context. This causes: (1) heap pressure from per-task QUIC/TLS state; (2) CPU pressure from partial TLS 1.3 key-schedule work on each accepted `Incoming`; (3) event-loop polling overhead from a large JoinSet in the `select!` branch, delaying processing of `connect_queue`, `outbound_connecting`, and topology changes; (4) degraded or dropped connection attempts from legitimate peers, disrupting P2P artifact delivery (blocks, ingress, state sync) and potentially stalling consensus progress on the subnet.

This maps to the allowed High impact: **Application/platform-level DoS, consensus blocking, or subnet availability impact not based on raw volumetric DDoS** — the attack exploits a specific application-level resource management flaw (unbounded JoinSet) rather than raw bandwidth saturation.

## Likelihood Explanation
The attacker is a single Byzantine subnet node — a standard below-threshold fault model for IC subnets. The node already holds a valid TLS certificate issued by the IC CA, satisfying the `SomeOrAllNodes::Some(subnet_node_set)` filter that is enforced only after the TLS handshake completes. Initiating thousands of QUIC connections per second from a single host using any standard QUIC library (e.g., Quinn itself) requires no special privileges beyond subnet membership. The attack is repeatable: after `CONNECT_TIMEOUT` drains the JoinSet, the attacker immediately re-floods.

## Recommendation
1. **Cap `inbound_connecting` before spawning**: In `handle_inbound_conn_attemp`, check `self.inbound_connecting.len()` against a configurable maximum (e.g., `2 × subnet_size`) and call `incoming.refuse()` if the cap is exceeded.
2. **Set `ServerConfig::concurrent_connections`**: Configure Quinn's built-in pending-connection limit to bound the number of `Incoming` objects surfaced before the application layer.
3. **Per-source-IP rate limiting**: Track in-flight inbound tasks per source IP and reject excess attempts before spawning.
4. **Move identity check earlier**: Use Quinn's `Incoming::accept_with` or a custom `ServerConfig` verifier to reject non-subnet peers at the QUIC handshake level, before a task is spawned.

## Proof of Concept
```
1. Attacker node (valid subnet member, valid TLS cert) opens a Quinn QUIC client endpoint.
2. In a tight loop, attacker calls endpoint.connect() to victim's endpoint address
   10,000 times, each with a fresh connection ID. After the QUIC Initial exchange
   completes (ClientHello sent), attacker drops the connection object on its side
   without sending TLS Certificate/Finished — stalling the server-side handshake.
3. Victim's endpoint.accept() surfaces 10,000 Incoming objects.
4. handle_inbound_conn_attemp spawns 10,000 tasks into inbound_connecting with no guard.
5. Assert: inbound_connecting.len() grows to ~10,000 (observable via the
   `connecting_connections` metric at L374–375).
6. Measure: event-loop latency for a legitimate peer's connection attempt increases
   by >1 second; artifact delivery to that peer stalls.
7. After CONNECT_TIMEOUT (10 s), tasks drain — attacker immediately re-floods.
Reproducible as a local integration test using turmoil or PocketIC with two
QuicTransport instances, one acting as the Byzantine flooder.
```