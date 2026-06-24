Audit Report

## Title
Unbounded Ingress-Window Loop in ICP Rosetta `construction_payloads` Causes OOM Process Crash — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
The ICP Rosetta `construction_payloads` handler iterates over an attacker-supplied `[ingress_start, ingress_end)` window with no upper-bound guard. Supplying `ingress_start=0` and `ingress_end=u64::MAX` causes the loop to execute ~153 million iterations before `Time::add_assign` wraps `now` back to a small value via a truncating `as u64` cast, making the loop infinite and exhausting all available heap memory, crashing the Rosetta process.

## Finding Description
The loop at `construction_payloads.rs` lines 99–107 iterates with a fixed `interval` of 120 seconds (120,000,000,000 ns), computed as `MAX_INGRESS_TTL (300s) - PERMITTED_DRIFT (60s) - 120s`:

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;
}
```

`ingress_start` and `ingress_end` are deserialized directly from the JSON metadata body as raw `Option<u64>` nanosecond values in `models.rs` lines 199–223, with no validation of the window size before the loop is entered.

The `Time::add_assign` implementation at `time.rs` lines 55–58 delegates to `from_duration` at lines 102–105, which performs `t.as_nanos() as u64` — a truncating cast from `u128` to `u64`. When `now` approaches `u64::MAX`, the addition `Duration::from_nanos(now) + interval` produces a `u128` value exceeding `2^64`, which truncates back to a small value (approximately `interval - 1` ns ≈ 120s), still less than `u64::MAX`. This makes the loop infinite rather than merely very long.

With `ingress_start=0` and `ingress_end=u64::MAX`:
- Iterations before first wrap: `u64::MAX / 120_000_000_000 ≈ 153,722,867`
- Memory allocated before wrap: `153M × 8 bytes ≈ 1.2 GB` → OOM crash
- After wrap, `now ≈ 120s` which is still `< u64::MAX`, so the loop restarts infinitely

The ICRC1 Rosetta equivalent at `rs/rosetta-api/icrc1/src/construction_api/services.rs` lines 148–158 has explicit guards (`ingress_start >= ingress_end` and `ingress_end < now + ingress_interval`) before its equivalent loop. The ICP Rosetta handler has no such guards.

## Impact Explanation
An unauthenticated attacker can crash the ICP Rosetta server process with a single HTTP request. Since Rosetta is a single-process service, this constitutes a complete denial of service of the ICP Rosetta node. This matches the allowed High impact: "Application/platform-level DoS, crash" and "Significant Rosetta... infrastructure security impact with concrete user or protocol harm." The Rosetta API is explicitly listed as in-scope under Financial integrations.

## Likelihood Explanation
No authentication is required for the Rosetta Construction API. The exploit requires a single HTTP POST to `/construction/payloads` with two integer fields set to `0` and `18446744073709551615`. The `ic_limits` constants confirm the interval is exactly 120s, making the iteration count and memory consumption precisely calculable. The attack is trivially repeatable after any process restart.

## Recommendation
Add a maximum ingress-window size check before the loop, mirroring the ICRC1 Rosetta implementation. For example:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request("ingress_start must be before ingress_end"));
}
let max_window = interval * 1440; // cap at ~48h
if ingress_end.saturating_duration_since(ingress_start) > max_window {
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

Alternatively, use `Time::checked_add` in the loop body and break or return an error on overflow, or cap `ingress_expiries` to a fixed maximum count.

## Proof of Concept
```rust
// Unit test: call construction_payloads with ingress_start=0, ingress_end=u64::MAX
let req = ConstructionPayloadsRequest {
    network_identifier: ...,
    operations: vec![/* valid transfer op */],
    metadata: Some(serde_json::json!({
        "ingress_start": 0u64,
        "ingress_end": u64::MAX,
    }).as_object().unwrap().clone()),
    public_keys: Some(vec![/* valid pk */]),
};
let result = handler.construction_payloads(req);
assert!(result.is_err(), "must reject unbounded ingress window");
```

Or via HTTP:
```bash
curl -X POST http://rosetta:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{"network_identifier":{...},"operations":[...],"public_keys":[...],"metadata":{"ingress_start":0,"ingress_end":18446744073709551615}}'
```