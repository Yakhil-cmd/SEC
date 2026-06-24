Audit Report

## Title
Unbounded Memory Allocation in CUP Response Body Collection Allows Byzantine Peer to OOM-Crash Orchestrator - (File: `rs/orchestrator/src/catch_up_package_provider.rs`)

## Summary
The `fetch_catch_up_package` function collects the full HTTP response body into an unbounded in-memory buffer, guarded only by a time-based timeout. A Byzantine subnet peer with a valid registry-registered TLS certificate can stream a multi-gigabyte response within the timeout window, exhausting the orchestrator's heap and crashing the process. Because the crash is repeatable on every restart cycle, the attacker can keep the target node permanently offline.

## Finding Description
At line 349 of `rs/orchestrator/src/catch_up_package_provider.rs`, the response body is accumulated with no byte-size cap:

```rust
let body_req = timeout(self.backoff, res.into_body().collect());
```

`http_body_util::BodyExt::collect()` accumulates every incoming chunk into a single `Collected<Bytes>` buffer with no upper bound. The wrapping `timeout(self.backoff, …)` only cancels the future if the *entire* transfer takes longer than `self.backoff` (initially 30 s). A peer streaming at ≥ 100 MB/s can deliver ≥ 3 GB before the timeout fires, all landing in the orchestrator heap.

No size guard exists anywhere in this code path — no `Limited::new`, no `content-length` check, no `max_response_size`. The contrast with the HTTPS outcalls adapter is direct: that code wraps the same `collect()` call with `http_body_util::Limited::new(body, remaining_limit as usize)` enforcing a hard byte cap before accumulation (`rs/https_outcalls/adapter/src/rpc_server.rs`, lines 420–422).

The TLS client is built per-node-id from the IC registry (`crypto_tls_config.client_config(*node_id, registry_version)`), so only a node with a valid registry-registered certificate can complete the handshake and serve a response — but this is exactly the Byzantine peer model. The peer selection logic (lines 183–199) means the victim node will contact the attacker's node directly when it is selected as one of the up-to-2 peers tried per cycle.

## Impact Explanation
The orchestrator process is killed by the OOM killer. Because the orchestrator is responsible for detecting replica version changes and triggering upgrades, a crashed orchestrator means the node cannot catch up to the current subnet height and cannot perform replica upgrades. A persistent attacker can re-trigger the crash on every restart cycle, keeping the node permanently offline. This maps to **High ($2,000–$10,000): Application/platform-level DoS, crash, or subnet availability impact not based on raw volumetric DDoS** — a single node is permanently stalled, degrading subnet fault tolerance.

## Likelihood Explanation
The attacker must control one registered subnet node (valid IC registry TLS certificate). This is within the IC's own Byzantine fault model (up to *f* malicious nodes out of 3*f*+1). A compromised or malicious node operator is a realistic threat. The attack is repeatable with no per-attempt cost beyond maintaining the streaming connection, and the victim node will periodically re-contact the attacker's node as part of normal CUP polling every ~10 seconds.

## Recommendation
Replace the bare `collect()` call with a size-limited variant, mirroring the pattern already used in the HTTPS outcalls adapter:

```rust
let body_req = timeout(
    self.backoff,
    http_body_util::Limited::new(res.into_body(), MAX_CUP_RESPONSE_BYTES)
        .collect(),
);
```

A reasonable cap is a few MB (real CUPs are small protobuf messages). The `LengthLimitError` should be mapped to a logged error and peer backoff, not a panic.

## Proof of Concept
1. Stand up a TLS server with a registry-registered node certificate.
2. Configure it to respond to `POST /_/catch_up_package` with HTTP 200 and a streaming body that emits data at ≥ 100 MB/s indefinitely.
3. Register this node as a subnet peer in a test registry.
4. Start the orchestrator pointing at this registry.
5. Observe: within 30 seconds the orchestrator process is killed by the OOM killer; no CUP is ever persisted; the node never advances.
6. Confirm fix: wrapping with `Limited::new(…, 4 * 1024 * 1024)` causes the body read to fail with `LengthLimitError` after 4 MB, the orchestrator logs an error, backs off, and remains alive.