Audit Report

## Title
Unbounded `ingress_expiries` Vec allocation via attacker-controlled `ingress_start`/`ingress_end` causes OOM crash — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

## Summary

The `construction_payloads` function in `rs/rosetta-api/icrc1/src/construction_api/services.rs` accepts user-supplied `ingress_start` and `ingress_end` as raw `u64` nanosecond timestamps with no bound on their range. An unauthenticated attacker can supply `ingress_start=0` and `ingress_end=u64::MAX`, causing the allocation loop to execute approximately 153 million iterations and allocate ~1.23 GB on the heap, crashing the Rosetta process via OOM.

## Finding Description

`construction_payloads` computes:
- `ingress_interval` = `(MAX_INGRESS_TTL − PERMITTED_DRIFT).as_nanos() as u64` = `(300s − 60s) × 10⁹` = **240,000,000,000 ns** (confirmed: `rs/limits/src/lib.rs` lines 17, 21)
- Step per iteration = `ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64)` = `240×10⁹ − 120×10⁹` = **120,000,000,000 ns** (confirmed: `rs/rosetta-api/icrc1/src/common/constants.rs` line 19)

The two guards before the loop (`rs/rosetta-api/icrc1/src/construction_api/services.rs` lines 148–158) are:

```rust
if ingress_start >= ingress_end { return Err(...) }       // line 148
if ingress_end < now + ingress_interval { return Err(...) } // line 154
```

With `ingress_start=0` and `ingress_end=u64::MAX`:
- Guard 1: `0 >= u64::MAX` → **false** → no error, continues
- Guard 2: `u64::MAX < now + ingress_interval` → **false** (u64::MAX ≈ 1.84×10¹⁹ >> now + 2.4×10¹¹) → no error, continues

The loop at lines 163–167 then runs:

```rust
while ingress_start < ingress_end {
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start +=
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
```

Iterations = `u64::MAX / 120,000,000,000 ≈ 153,722,867`. Each push is 8 bytes (a `u64`), totalling **≈1.23 GB** of heap allocation from a single request. The process OOMs before the loop completes.

The HTTP endpoint (`rs/rosetta-api/icrc1/src/construction_api/endpoints.rs` lines 90–107) performs no authentication — it only validates `network_identifier`. The `ingress_start` and `ingress_end` fields are plain `Option<u64>` in `ConstructionPayloadsRequestMetadata` (`rs/rosetta-api/icrc1/src/construction_api/types.rs` lines 188–195) with no range validation at the deserialization layer.

## Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with a crafted metadata payload causes the ICRC1 Rosetta server process to exhaust heap memory and crash (OOM kill or panic). Because ICRC1 Rosetta runs as a single process with no redundancy, this takes the entire Rosetta API offline. This matches the allowed High impact: **"Application/platform-level DoS, crash... or subnet availability impact not based on raw volumetric DDoS"** and **"Significant... Rosetta... security impact with concrete user or protocol harm"** ($2,000–$10,000).

## Likelihood Explanation

The endpoint is publicly reachable on any deployed ICRC1 Rosetta instance. Exploitation requires only knowledge of the Rosetta API spec and the ability to craft a JSON body — no credentials, no rate-limit bypass, and no volumetric traffic. A single request suffices. The `ingress_start`/`ingress_end` fields are documented in the metadata struct. Likelihood is **high**.

## Recommendation

Add an explicit upper bound on the computed range before entering the loop:

```rust
const MAX_INGRESS_WINDOW_NS: u64 = 24 * 3600 * 1_000_000_000; // 24 hours in ns

if ingress_end.saturating_sub(ingress_start) > MAX_INGRESS_WINDOW_NS {
    return Err(Error::processing_construction_failed(
        "Ingress window exceeds maximum allowed duration of 24 hours"
    ));
}
```

This caps `ingress_expiries` to at most `24h / 120s = 720` entries, consistent with the documented intent in `ConstructionPayloadsRequestMetadata` ("Must be within 24 hours from created_at_time").

## Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8082/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"<network_id>"},
    "operations": [/* valid transfer ops */],
    "public_keys": [/* valid pubkey */],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
```

Expected result: the Rosetta process OOMs and crashes before responding. A unit test can confirm this by calling `construction_payloads` directly with `ingress_start=0`, `ingress_end=u64::MAX`, and asserting that it returns an error (currently it does not — it proceeds to the loop).