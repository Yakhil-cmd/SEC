Audit Report

## Title
Integer Overflow in `construction_payloads` Loop Causes Infinite Loop / DoS - (File: `rs/rosetta-api/icrc1/src/construction_api/services.rs`)

## Summary
The `construction_payloads` function in the ICRC1 Rosetta API performs unchecked `u64` addition on user-controlled `ingress_start` and `ingress_end` values. When an attacker supplies values near `u64::MAX`, both existing validation checks pass, and the while-loop that builds `ingress_expiries` overflows and wraps `ingress_start` to a small value, causing an infinite loop that exhausts CPU and memory. In release builds with overflow checks disabled (the Rust default), the process hangs indefinitely; with overflow checks enabled, the addition panics and crashes the handler thread — either outcome constitutes a complete DoS of the ICRC1 Rosetta service.

## Finding Description
In `rs/rosetta-api/icrc1/src/construction_api/services.rs`, `ingress_interval` is computed as raw `u64` nanoseconds:

```rust
let ingress_interval: u64 =
    (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos() as u64;
``` [1](#0-0) 

User-supplied `ingress_start` and `ingress_end` are accepted directly as `u64` nanosecond timestamps: [2](#0-1) 

Two guards exist but neither prevents the overflow: [3](#0-2) 

The loop uses unchecked `u64` addition: [4](#0-3) 

**Attack trace with `ingress_start = u64::MAX - 1`, `ingress_end = u64::MAX`:**

1. **Check 1** (`ingress_start >= ingress_end`): `u64::MAX-1 >= u64::MAX` → false → passes.
2. **Check 2** (`ingress_end < now + ingress_interval`): `now` ≈ 1.7×10¹⁸ ns; `ingress_interval` ≈ 2.4×10¹¹ ns; `u64::MAX ≈ 1.8×10¹⁹ < 1.7×10¹⁸ + 2.4×10¹¹` → false → passes.
3. **Loop iteration 1**: `ingress_start + ingress_interval` = `(u64::MAX-1) + ~2.4×10¹¹` → wraps to a small value (release build) or panics (overflow-checks=true). `ingress_start +=` also wraps to a small value.
4. **Loop condition**: small value `< u64::MAX` → true → repeats forever (or panics on first iteration).

The ICP Rosetta handler avoids this by using `ic_types::time::Time`, whose `Add` implementation uses `saturating_add`: [5](#0-4) 

The ICRC1 path has no equivalent protection. The `INGRESS_INTERVAL_OVERLAP` constant is defined as `Duration::from_secs(120)`: [6](#0-5) 

## Impact Explanation
An attacker can render the ICRC1 Rosetta API completely unresponsive with a single HTTP request. The handler thread either loops infinitely consuming 100% CPU and unbounded memory (wrapping build), or panics and crashes (overflow-checked build). All subsequent ICRC1 token transfer operations routed through this Rosetta node are blocked. This matches the allowed impact: **High ($2,000–$10,000) — Significant Rosetta/financial-integration security impact with concrete user or protocol harm**, and also **High — Application/platform-level DoS not based on raw volumetric DDoS**.

## Likelihood Explanation
The `/construction/payloads` endpoint is unauthenticated and publicly reachable. The attack requires a single HTTP POST with two crafted integer fields (`ingress_start`, `ingress_end`). No special privileges, cryptographic keys, or prior knowledge of the system state are required. Any external party aware of the Rosetta API specification can trigger this. The attack is repeatable and deterministic.

## Recommendation
Replace the unchecked addition inside the loop with `checked_add`, and add an explicit pre-loop upper-bound validation:

```rust
if ingress_start > u64::MAX - ingress_interval {
    return Err(Error::processing_construction_failed(
        "ingress_start too large: would overflow when adding ingress_interval",
    ));
}
while ingress_start < ingress_end {
    let expiry = ingress_start
        .checked_add(ingress_interval)
        .ok_or_else(|| Error::processing_construction_failed("ingress expiry overflow"))?;
    ingress_expiries.push(expiry);
    ingress_start = ingress_start.saturating_add(
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64),
    );
}
```

Also fix the unchecked `now + ingress_interval` in the second validation check (line 154) with `now.saturating_add(ingress_interval)`, mirroring the safe pattern used in the ICP Rosetta handler.

## Proof of Concept

Send the following HTTP request to the ICRC1 Rosetta API:

```http
POST /construction/payloads HTTP/1.1
Content-Type: application/json

{
  "network_identifier": { ... },
  "operations": [ /* valid ICRC1 transfer operation */ ],
  "public_keys": [ /* valid public key */ ],
  "metadata": {
    "ingress_start": 18446744073709551614,
    "ingress_end":   18446744073709551615
  }
}
```

Both validation checks pass. In a release build without overflow checks, the while-loop at line 163 immediately wraps `ingress_start` to a small value on the first iteration and loops forever, growing `ingress_expiries` without bound. In a build with overflow checks enabled, the `+` at line 164 panics, crashing the handler. A minimal unit test reproducing the infinite-loop path:

```rust
#[test]
#[should_panic] // or run with a timeout to observe hang
fn test_overflow_dos() {
    use std::time::{Duration, SystemTime, UNIX_EPOCH};
    let metadata = ConstructionPayloadsRequestMetadata {
        ingress_start: Some(u64::MAX - 1),
        ingress_end: Some(u64::MAX),
        ..Default::default()
    };
    // now must be small enough that check 2 passes
    let now = UNIX_EPOCH + Duration::from_nanos(1_700_000_000_000_000_000);
    let _ = construction_payloads(vec![], Some(metadata), &Principal::anonymous(), vec![pk], now);
}
```

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L120-121)
```rust
    let ingress_interval: u64 =
        (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos() as u64;
```

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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L148-158)
```rust
    if ingress_start >= ingress_end {
        return Err(Error::processing_construction_failed(&format!(
            "Ingress start should start before ingress end: Start: {ingress_start}, End: {ingress_end}"
        )));
    }

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

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L99-107)
```rust
        let mut ingress_expiries = vec![];
        let mut now = ingress_start;
        while now < ingress_end {
            let ingress_expiry = (now
                + ic_limits::MAX_INGRESS_TTL.saturating_sub(ic_limits::PERMITTED_DRIFT))
            .as_nanos_since_unix_epoch();
            ingress_expiries.push(ingress_expiry);
            now += interval;
        }
```

**File:** rs/rosetta-api/icrc1/src/common/constants.rs (L19-19)
```rust
pub const INGRESS_INTERVAL_OVERLAP: Duration = Duration::from_secs(120);
```
