Audit Report

## Title
Unbounded While-Loop DoS via Attacker-Controlled `ingress_end = u64::MAX` in `construction_payloads` — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

## Summary
The `construction_payloads` function accepts unauthenticated HTTP POST requests and reads `ingress_end` directly from the request body without imposing an upper bound on the range `ingress_end - ingress_start`. Supplying `ingress_end = u64::MAX` bypasses both existing guards and causes the subsequent while-loop to execute approximately 139 million iterations, exhausting ~1.1 GB of memory (OOM kill in debug/release) or entering an infinite loop (release mode after u64 wrap-around), permanently denying service to all Rosetta API users.

## Finding Description
In `services.rs` at line 111, `construction_payloads` reads `ingress_start` and `ingress_end` from the unauthenticated request metadata: [1](#0-0) 

Two guards exist. Guard 1 (line 148) rejects `ingress_start >= ingress_end`: [2](#0-1) 

Guard 2 (line 154) rejects `ingress_end < now + ingress_interval`: [3](#0-2) 

Neither guard imposes an upper bound on the range. With `ingress_end = u64::MAX`, both checks pass trivially (any `ingress_start < u64::MAX` satisfies Guard 1; `u64::MAX` is always ≥ `now + ingress_interval` for any realistic `now`). The loop then runs uncapped: [4](#0-3) 

The loop step is `ingress_interval - INGRESS_INTERVAL_OVERLAP`:
- `ingress_interval = (MAX_INGRESS_TTL - PERMITTED_DRIFT).as_nanos() = (300s - 60s) = 240,000,000,000 ns` [5](#0-4) 

- `INGRESS_INTERVAL_OVERLAP = 120,000,000,000 ns` [6](#0-5) 

Step = `240,000,000,000 - 120,000,000,000 = 120,000,000,000 ns`. With `ingress_start ≈ 1.75×10¹⁸ ns` (current epoch) and `ingress_end = u64::MAX ≈ 1.844×10¹⁹ ns`:

```
iterations ≈ (1.844e19 - 1.75e18) / 1.2e11 ≈ 139,000,000
memory     ≈ 139,000,000 × 8 bytes ≈ 1.1 GB
```

In release mode, when `ingress_start` nears `u64::MAX`, the wrapping addition `ingress_start += 120_000_000_000` wraps to a small value, making `ingress_start < u64::MAX` true again — producing an infinite loop. In debug mode, the overflow panics, but only after OOM is likely already triggered.

## Impact Explanation
A single unauthenticated HTTP POST to `/construction/payloads` with `ingress_end = 18446744073709551615` causes the ICRC1 Rosetta server process to either be OOM-killed (~1.1 GB allocation) or enter an infinite CPU loop (release build). This is a complete, persistent denial-of-service of the ICRC1 Rosetta API — all users relying on it for transaction construction are blocked until the process is manually restarted. This matches the allowed High impact: **"Significant Rosetta, boundary/API, or infrastructure security impact with concrete user or protocol harm"** and **"Application/platform-level DoS not based on raw volumetric DDoS"**.

## Likelihood Explanation
No authentication is required for `/construction/payloads` — it is a public Rosetta API endpoint by design. The payload is trivial to construct (a valid JSON body with one field set to `18446744073709551615`). The attack is single-request, requires no prior state, no special privileges, and is immediately and indefinitely repeatable after each server restart.

## Recommendation
Add an explicit upper-bound check on the range before entering the loop. For example:

```rust
// Cap the ingress window to a reasonable maximum (e.g., 24 hours)
const MAX_INGRESS_EXPIRY_COUNT: u64 = 720; // 720 × 120s = 24h
let max_range = ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64)
    .saturating_mul(MAX_INGRESS_EXPIRY_COUNT);
if ingress_end.saturating_sub(ingress_start) > max_range {
    return Err(Error::processing_construction_failed(
        &"Ingress window exceeds maximum allowed range",
    ));
}
```

Alternatively, break out of the loop after pushing `MAX_INGRESS_EXPIRY_COUNT` entries.

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
        ingress_end: Some(u64::MAX),  // attacker-controlled
        created_at_time: None,
        memo: None,
    }),
    &some_principal,
    vec![valid_public_key()],
    now,
);
// Release build: never returns (infinite loop after u64 wrap)
// Debug build: OOM or overflow panic after ~139M iterations
```

A unit test can be added directly to the existing `#[cfg(test)]` block in `services.rs` calling `construction_payloads` with `ingress_end = Some(u64::MAX)` and asserting the call returns an error (currently it does not — it hangs or panics).

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L128-136)
```rust
    let mut ingress_start = metadata
        .as_ref()
        .and_then(|meta| meta.ingress_start)
        .unwrap_or(now);

    let ingress_end = metadata
        .as_ref()
        .and_then(|meta| meta.ingress_end)
        .unwrap_or(ingress_start + ingress_interval);
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L148-152)
```rust
    if ingress_start >= ingress_end {
        return Err(Error::processing_construction_failed(&format!(
            "Ingress start should start before ingress end: Start: {ingress_start}, End: {ingress_end}"
        )));
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L154-158)
```rust
    if ingress_end < now + ingress_interval {
        return Err(Error::processing_construction_failed(&format!(
            "Ingress end should be at least one interval from the current time: Current time: {now}, End: {ingress_end}"
        )));
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L163-167)
```rust
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
```

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

**File:** rs/rosetta-api/icrc1/src/common/constants.rs (L19-19)
```rust
pub const INGRESS_INTERVAL_OVERLAP: Duration = Duration::from_secs(120);
```
