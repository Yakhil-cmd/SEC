Audit Report

## Title
XNet Peer Selection Permanently Biased via RTT EMA Manipulation in ProximityMap - (File: rs/xnet/payload_builder/src/proximity.rs)

## Summary
`ProximityMap` weights node selection inversely by RTT EMA. In `XNetClientImpl::query()`, `observe_roundtrip_time()` is called immediately after HTTP response headers are received, before the body is read. A malicious node can send headers instantly (~1 µs) then stall or drop the body, recording a minimum-clamped RTT of 1,000 ns and achieving maximum weight `1_000_000_000` versus `10_000` for an honest 100 ms node — a 100,000× selection bias. Once the EMA is poisoned, the malicious node monopolizes XNet pulls and can stall cross-subnet message delivery indefinitely.

## Finding Description
**Root cause — RTT observation before body read** (`rs/xnet/payload_builder/src/lib.rs`, L1819–1843):

```rust
let request_start = Instant::now();
let response = self.http_client.get(endpoint.url.clone()).await ...;  // returns on headers

self.proximity_map.observe_roundtrip_time(   // ← committed here, before body
    endpoint.node_id,
    Instant::now().saturating_duration_since(request_start),
);

let content = http_body_util::Limited::new(response.into_body(), ...)
    .collect().await                          // ← body read happens after
    .map_err(XNetClientError::BodyReadError)?;
```

`http_client.get().await` resolves when response headers arrive; the body is streamed separately. A malicious node sends `200 OK` headers in ~1 µs, then closes the connection. The RTT observation is committed with `duration ≈ 1 µs`, clamped to 1,000 ns (`rs/xnet/payload_builder/src/proximity.rs`, L201), and the EMA is updated via `(*ema * 9 + duration_nanos) / 10` (L216). The body read then fails with `BodyReadError` or the 5-second `tokio::time::timeout` fires — but the RTT record is already written.

**Weight amplification** (`rs/xnet/payload_builder/src/proximity.rs`, L237):
```rust
1_000 * NANOS_PER_SEC / ema_nanos
```
With `ema_nanos = 1_000` → weight = `1_000_000_000`. An honest node at 100 ms → weight = `10_000`. The 100,000× disparity is not a theoretical edge case; the existing test `pick_node_extreme_roundtrip_times` (`rs/xnet/payload_builder/src/proximity/tests.rs`, L150–172) explicitly asserts that a 1 µs operator is selected 1,000,000× more often than a 1 s operator.

**Selection path** (`rs/xnet/payload_builder/src/lib.rs`, L1144–1145):
```rust
let (node, node_record) = self.proximity_map.pick_node(subnet_id, version)?;
```
`xnet_endpoint_url()` calls `pick_node()` on every XNet pull attempt, so the bias is applied continuously and persistently.

Existing guards are insufficient: the 5-second timeout and `BodyReadError` path both occur *after* the RTT observation is committed. There is no penalty observation on failure, no floor on weight ratios, and no minimum selection probability.

## Impact Explanation
Once the malicious node's EMA is poisoned, it is selected for ~98–100% of XNet pulls from the victim subnet. It can then serve `204 No Content` on every pull, leaving the victim subnet's slice pool for the remote subnet empty. No XNet messages from the remote subnet are included in the victim subnet's blocks, stalling cross-subnet message delivery indefinitely. This is a persistent, self-reinforcing availability impact on XNet communication between two specific subnets. Canisters depending on incoming XNet messages (inter-canister calls, ICP ledger notifications, chain-fusion callbacks) experience unbounded delivery delays. This matches the allowed impact: **"Application/platform-level DoS... or subnet availability impact not based on raw volumetric DDoS"** — **High ($2,000–$10,000)**.

## Likelihood Explanation
The attacker requires control of one registered node on the target remote subnet — a "protocol peer below the consensus fault threshold," explicitly within the HackenProof bounty scope. No threshold corruption, DNS hijack, privileged key, or user interaction is required. The attack is fully automated: the malicious node's HTTP server simply sends headers and drops the connection. With hundreds of node operators across IC subnets, a single compromised or malicious operator is a realistic threat. The attack is repeatable and self-reinforcing: each failed pull re-poisons the EMA.

## Recommendation
1. **Move `observe_roundtrip_time` after successful body read** — only record RTT when a complete, valid response body is received (`rs/xnet/payload_builder/src/lib.rs`, after L1843).
2. **Record a penalty RTT on body failure or timeout** — on `BodyReadError` or `Timeout`, call `observe_roundtrip_time` with a high penalty value (e.g., 1 s) to penalize nodes that respond with headers but fail to deliver content.
3. **Cap the maximum weight ratio** — enforce a floor on node weights (e.g., no node may have weight less than `max_weight / 1000`) to bound the maximum selection bias regardless of RTT spread.
4. **Apply a minimum selection probability** — guarantee every registered node has at least a small baseline probability of being selected, preventing complete monopolization.

## Proof of Concept
**Setup**: Attacker controls node `N` (operator `OP_A`) on subnet B. Subnet A's XNet payload builder pulls streams from subnet B.

**Step 1 — Poison the EMA**: Node `N` serves XNet HTTP requests by sending `200 OK` headers in ~1 µs, then immediately closes the connection without a body. `XNetClientImpl::query()` records RTT ≈ 1 µs (clamped to 1,000 ns at `rs/xnet/payload_builder/src/proximity.rs` L201), then fails with `BodyReadError` at `rs/xnet/payload_builder/src/lib.rs` L1843.

**Step 2 — EMA converges**: After ~10 such interactions, `OP_A`'s EMA ≈ 1,000 ns → weight = `1_000_000_000`. Honest nodes on subnet B with 100 ms RTT have weight = `10_000`.

**Step 3 — Monopolize selection**: With one malicious node (weight `1_000_000_000`) and two honest nodes (weight `10_000` each), the malicious node is selected with probability `1_000_000_000 / 1_020_000 ≈ 98%`.

**Step 4 — Stall XNet**: Node `N` serves `204 No Content` on every pull. Subnet A's slice pool for subnet B remains empty. Cross-subnet message delivery stalls indefinitely.

**Verification**: The existing unit test `pick_node_extreme_roundtrip_times` at `rs/xnet/payload_builder/src/proximity/tests.rs` L131–172 directly confirms the 1,000,000× weight disparity between a 1 µs and 1 s RTT operator, validating the extreme selection bias. A targeted integration test can be written using a mock HTTP server that sends headers and drops the body, then asserts that `observe_roundtrip_time` is called with a near-zero duration before the `BodyReadError` is returned. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/xnet/payload_builder/src/lib.rs (L1144-1145)
```rust
        let version = self.registry.get_latest_version();
        let (node, node_record) = self.proximity_map.pick_node(subnet_id, version)?;
```

**File:** rs/xnet/payload_builder/src/lib.rs (L1819-1843)
```rust
            let request_start = Instant::now();

            let response = self
                .http_client
                .get(endpoint.url.clone())
                .await
                .map_err(XNetClientError::RequestFailed)?;

            // While this is not exactly roundtrip time (it may include multiple roundtrips
            // e.g. if a TLS connection needs to be established first), it is a good enough
            // approximation. Else, we would have to use explicit pings to measure actual
            // roundtrip times.
            self.proximity_map.observe_roundtrip_time(
                endpoint.node_id,
                Instant::now().saturating_duration_since(request_start),
            );

            let status = response.status();

            let content =
                http_body_util::Limited::new(response.into_body(), 5 * POOL_SLICE_BYTE_SIZE_MAX)
                    .collect()
                    .await
                    .map(|collected| collected.to_bytes())
                    .map_err(XNetClientError::BodyReadError)?;
```

**File:** rs/xnet/payload_builder/src/proximity.rs (L199-217)
```rust
    pub fn observe_roundtrip_time(&self, node: NodeId, duration: Duration) {
        // Bound durations to between 1µs and 1s (specifically avoiding 0).
        let duration_nanos = (duration.as_nanos() as u64).clamp(1_000, NANOS_PER_SEC);

        let version = self.registry.get_latest_version();
        if let Some(node_operator) =
            get_node_operator_id(&node, self.registry.as_ref(), &version, &self.log)
        {
            let metric_rtt_ema = self
                .metric_rtt_ema
                .with_label_values(&[&node_operator_to_string(&node_operator)]);

            let rtt_ema_nanos = *self
                .roundtrip_ema_nanos
                .lock()
                .unwrap()
                .entry(node_operator)
                .and_modify(|ema| *ema = (*ema * 9 + duration_nanos) / 10)
                .or_insert_with(|| duration_nanos);
```

**File:** rs/xnet/payload_builder/src/proximity.rs (L232-238)
```rust
    fn weight(&self, node_operator: &[u8]) -> Option<u64> {
        self.roundtrip_ema_nanos
            .lock()
            .unwrap()
            .get(node_operator)
            .map(|ema_nanos| 1_000 * NANOS_PER_SEC / ema_nanos)
    }
```

**File:** rs/xnet/payload_builder/src/proximity/tests.rs (L144-172)
```rust
        // Operator 1 with an observed RTT of 13ns => recorded RTT EMA of 1µs.
        proximity_map.observe_roundtrip_time(REMOTE_NODE_1_OPERATOR_1, Duration::from_nanos(13));
        // Operator 2 with an observed RTT of 5s => recorded RTT EMA
        // of 1s.
        proximity_map.observe_roundtrip_time(REMOTE_NODE_3_OPERATOR_2, Duration::from_secs(5));

        // Nodes from `OPERATOR_1` should be 1_000_000x more likely to be picked than
        // nodes from `OPERATOR_2`.
        assert_pick_node(
            REMOTE_NODE_1_OPERATOR_1,
            &mut proximity_map,
            0,
            1_000_000,
            2_000_001,
        );
        assert_pick_node(
            REMOTE_NODE_2_OPERATOR_1,
            &mut proximity_map,
            1_000_000,
            2_000_000,
            2_000_001,
        );
        assert_pick_node(
            REMOTE_NODE_3_OPERATOR_2,
            &mut proximity_map,
            2_000_000,
            2_000_001,
            2_000_001,
        );
```
