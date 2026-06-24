The code matches the citations exactly. Let me verify the arithmetic and guard logic against the actual source.

**Guard 1** (`L148`): `ingress_start >= ingress_end` — with `ingress_start = now ≈ 1.75e18` and `ingress_end = u64::MAX`, this is false → passes.

**Guard 2** (`L154`): `ingress_end < now + ingress_interval` — `now + 240e9 ≈ 1.75e18`, which is far less than `u64::MAX` → false → passes.

**Loop** (`L163-167`): step = `ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP) = 240e9 - 120e9 = 120e9 ns`. The `+=` on `ingress_start` is plain (not saturating), so in release mode it wraps past `u64::MAX` back to a small value, making `ingress_start < u64::MAX` true again → infinite loop. In debug mode, the overflow panics after ~139M iterations and ~1.1 GB of allocation.

All cited constants (`MAX_INGRESS_TTL = 300s`, `PERMITTED_DRIFT = 60s`, `INGRESS_INTERVAL_OVERLAP = 120s`) are confirmed in the actual files. The exploit path is fully confirmed.

---

Audit Report

## Title
Unbounded While-Loop DoS via Attacker-Controlled `ingress_end = u64::MAX` in `construction_payloads` — (File: `rs/rosetta-api/icrc1/src/construction_api/services.rs`)

## Summary
The `construction_payloads` function accepts an unauthenticated HTTP POST request containing `ingress_end` as a caller-supplied `u64`. Neither of the two existing input guards imposes an upper bound on the range `ingress_end - ingress_start`. Setting `ingress_end = u64::MAX` causes the subsequent while-loop to execute approximately 139 million iterations in debug mode (exhausting ~1.1 GB of memory before an overflow panic) or to spin infinitely in release mode (due to unsigned integer wraparound), completely denying service to the ICRC1 Rosetta server.

## Finding Description
In `services.rs` at `construction_payloads` (L111), `ingress_start` and `ingress_end` are read directly from the unauthenticated request body (L128–136). Two guards follow:

- **Guard 1** (L148–152): rejects `ingress_start >= ingress_end`. With `ingress_start = now` and `ingress_end = u64::MAX`, this is trivially false and does not fire.
- **Guard 2** (L154–158): rejects `ingress_end < now + ingress_interval`. With `ingress_end = u64::MAX` and `now + ingress_interval ≈ 1.75e18`, the condition is false and does not fire.

Neither guard bounds the *range* `ingress_end - ingress_start`. The loop (L163–167) then runs:

```rust
while ingress_start < ingress_end {
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start +=
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
```

- `ingress_interval = (MAX_INGRESS_TTL − PERMITTED_DRIFT).as_nanos() = 240,000,000,000 ns`
- `INGRESS_INTERVAL_OVERLAP = 120,000,000,000 ns`
- Effective step = `120,000,000,000 ns`

With `ingress_start ≈ 1.75×10¹⁸` and `ingress_end = u64::MAX ≈ 1.844×10¹⁹`, the loop runs ≈ `(1.844e19 − 1.75e18) / 1.2e11 ≈ 139,000,000` iterations before `ingress_start` overflows. The `+=` operator is plain (not saturating), so in **release mode** `ingress_start` wraps to a small value and the condition `ingress_start < u64::MAX` becomes true again — producing an **infinite loop**. In **debug mode**, the addition panics after OOM (~1.1 GB from `139M × 8 bytes` pushed to `ingress_expiries`).

## Impact Explanation
A single unauthenticated HTTP POST to `/construction/payloads` with `ingress_end = 18446744073709551615` either OOM-kills the ICRC1 Rosetta server process or locks it in an infinite CPU loop. All users relying on the Rosetta API for ICRC1 transaction construction are denied service until the process is manually restarted. This matches the allowed ICP bounty impact: **High ($2,000–$10,000) — Significant Rosetta/ledger infrastructure security impact with concrete user harm** (application-level DoS of a financial integration component).

## Likelihood Explanation
No authentication is required for `/construction/payloads` — it is a public Rosetta API endpoint by design. The malicious payload is a single valid JSON body with one field set to the maximum `u64` value. The attack requires no prior state, no special privileges, and is immediately and indefinitely repeatable by any external party.

## Recommendation
Add an explicit upper-bound check on the ingress window before entering the loop. For example:

```rust
let max_ingress_range = ingress_interval * 1440; // cap at ~48h
if ingress_end.saturating_sub(ingress_start) > max_ingress_range {
    return Err(Error::processing_construction_failed(
        &"Ingress window exceeds maximum allowed range",
    ));
}
```

Alternatively, cap the number of entries appended to `ingress_expiries` with a hard limit (e.g., break after 1440 entries). Also change the `ingress_start + ingress_interval` push on L164 to a saturating add to prevent overflow-induced UB in release mode.

## Proof of Concept

```rust
use std::time::SystemTime;

let now = SystemTime::now();
let result = construction_payloads(
    valid_operations(),
    Some(ConstructionPayloadsRequestMetadata {
        ingress_start: Some(
            now.duration_since(SystemTime::UNIX_EPOCH).unwrap().as_nanos() as u64
        ),
        ingress_end: Some(u64::MAX), // attacker-controlled
        created_at_time: None,
        memo: None,
    }),
    &some_principal,
    vec![valid_public_key()],
    now,
);
// Release build: never returns (infinite loop after wraparound)
// Debug build: OOM or overflow panic after ~139M iterations
```

A minimal unit test in `services.rs` `#[cfg(test)]` calling `construction_payloads` with `ingress_end = u64::MAX` and observing either non-termination (release) or panic (debug) is sufficient to reproduce deterministically without any network access.