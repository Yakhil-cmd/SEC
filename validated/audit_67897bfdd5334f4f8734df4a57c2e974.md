### Title
XNet Peer Selection Permanently Biased via RTT EMA Manipulation in ProximityMap - (File: rs/xnet/payload_builder/src/proximity.rs)

---

### Summary

The `ProximityMap::pick_node()` function selects remote-subnet nodes for XNet stream pulls using RTT-weighted random selection. Because `observe_roundtrip_time()` is called immediately after receiving HTTP response **headers** — before the response body is read — a malicious node on a remote subnet can record an artificially minimal RTT (~1 µs) by sending response headers instantly while delaying or withholding the body. This drives its selection weight to the maximum (`1_000_000_000`) versus `10_000` for an honest 100 ms node, a 100,000× bias. The malicious node then monopolizes XNet pulls and can stall cross-subnet message delivery indefinitely.

---

### Finding Description

`ProximityMap` in `rs/xnet/payload_builder/src/proximity.rs` maintains a per-datacenter-operator EMA of roundtrip times and uses it to weight node selection. The weight formula is:

```
weight = 1_000 * NANOS_PER_SEC / ema_nanos
``` [1](#0-0) 

RTT is clamped to `[1_000 ns, 1_000_000_000 ns]`, so weights range from `1_000` (1 s RTT) to `1_000_000_000` (1 µs RTT). [2](#0-1) 

In `XNetClientImpl::query()`, the RTT observation is recorded **immediately after receiving HTTP response headers**, before the response body is read:

```rust
let response = self.http_client.get(endpoint.url.clone()).await ...;

self.proximity_map.observe_roundtrip_time(   // ← RTT recorded here
    endpoint.node_id,
    Instant::now().saturating_duration_since(request_start),
);

// body read happens AFTER, and may timeout
let content = http_body_util::Limited::new(response.into_body(), ...)
    .collect().await ...;
``` [3](#0-2) 

A malicious node can exploit this ordering by:
1. Sending HTTP response headers immediately (RTT ≈ 1 µs → minimum clamp → maximum weight `1_000_000_000`)
2. Then stalling or closing the body stream, causing a `BodyReadError` or the 5-second timeout to fire

The RTT observation is already committed before the body failure. The EMA update formula `(*ema * 9 + duration_nanos) / 10` means even a single 1 µs observation rapidly drives the EMA toward the minimum: [4](#0-3) 

After a handful of such interactions, the malicious node's weight is `1_000_000_000` while an honest 100 ms node has weight `10_000` — a **100,000× selection bias**. The `pick_node()` weighted random selection then almost exclusively picks the malicious node: [5](#0-4) 

The `xnet_endpoint_url()` function in `XNetEndpointResolver` calls `pick_node()` on every XNet pull attempt, so the bias is applied continuously: [6](#0-5) 

---

### Impact Explanation

Once the malicious node monopolizes selection, it can serve `204 No Content` responses on every pull. The victim subnet's XNet payload builder finds no new slices to include in blocks, stalling cross-subnet message delivery from the remote subnet indefinitely. Canisters on the victim subnet that depend on incoming XNet messages (e.g., inter-canister calls, ICP ledger notifications, chain-fusion callbacks) experience unbounded delivery delays. This is a targeted availability impact on XNet communication between two specific subnets, not a global outage, but it is persistent and self-reinforcing once the EMA is poisoned.

---

### Likelihood Explanation

The attacker needs control of **one registered node** on the target remote subnet — a "protocol peer below the consensus fault threshold." Node operators are permissioned via NNS governance, but the IC has hundreds of node operators across many subnets. A single compromised or malicious node operator is a realistic threat model explicitly listed in the HackenProof scope ("protocol peer below the consensus fault threshold," "xnet origin"). No threshold corruption, DNS hijack, or privileged key is required. The attack is fully automated and requires no user interaction.

---

### Recommendation

1. **Move `observe_roundtrip_time` after successful body read** — only record RTT when a complete, valid response body is received. This prevents header-only fast-path manipulation.
2. **Record a penalty RTT on body failure or timeout** — treat `BodyReadError` and `Timeout` as a high-RTT observation (e.g., 1 s) to penalize nodes that respond with headers but fail to deliver content.
3. **Cap the maximum weight ratio** — enforce a floor on node weights (e.g., no node may have weight less than `max_weight / 1000`) to bound the maximum selection bias regardless of RTT spread.
4. **Apply a minimum selection probability** — guarantee every registered node on a subnet has at least a small baseline probability of being selected, preventing complete monopolization.

---

### Proof of Concept

**Setup**: Attacker controls node `N` (operator `OP_A`) on subnet B. Subnet A's XNet payload builder pulls streams from subnet B.

**Step 1 — Poison the EMA**: Node `N` serves XNet HTTP requests by sending response headers with status 200 in ~1 µs, then immediately closes the connection without a body. `XNetClientImpl::query()` records RTT ≈ 1 µs (clamped to 1,000 ns), then fails with `BodyReadError`.

**Step 2 — EMA converges**: After ~10 such interactions, `OP_A`'s EMA ≈ 1,000 ns → weight = `1_000 * 1_000_000_000 / 1_000 = 1_000_000_000`. Honest nodes on subnet B with 100 ms RTT have weight = `10_000`.

**Step 3 — Monopolize selection**: `pick_node()` draws a random value in `[1, total_weight]`. With one malicious node (weight `1_000_000_000`) and two honest nodes (weight `10_000` each), the malicious node is selected with probability `1_000_000_000 / 1_020_000 ≈ 98%`. [7](#0-6) 

**Step 4 — Stall XNet**: Node `N` now serves `204 No Content` on every pull. Subnet A's slice pool for subnet B remains empty. No XNet messages from subnet B are included in subnet A's blocks. Cross-subnet message delivery stalls indefinitely.

**Verification**: The test `pick_node_extreme_roundtrip_times` in `rs/xnet/payload_builder/src/proximity/tests.rs` explicitly confirms that a 1 µs RTT node is selected 1,000,000× more often than a 1 s RTT node, validating the extreme weight disparity: [8](#0-7)

### Citations

**File:** rs/xnet/payload_builder/src/proximity.rs (L169-183)
```rust
        // Cumulative node weights, to be used for weighted random selection.
        let cumulative_weights: Vec<u64> = node_weights
            .into_iter()
            .map(|weight| if weight != 0 { weight } else { mean_weight })
            .scan(0, |accumulator, weight| {
                (*accumulator) += weight;
                Some(*accumulator)
            })
            .collect();
        let total_weight = *cumulative_weights.last().unwrap();

        // Pick a random node by weight.
        let node_index = cumulative_weights
            .binary_search(&(self.gen_range)(1, total_weight + 1))
            .unwrap_or_else(|e| e);
```

**File:** rs/xnet/payload_builder/src/proximity.rs (L200-201)
```rust
        // Bound durations to between 1µs and 1s (specifically avoiding 0).
        let duration_nanos = (duration.as_nanos() as u64).clamp(1_000, NANOS_PER_SEC);
```

**File:** rs/xnet/payload_builder/src/proximity.rs (L211-217)
```rust
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
