All four files are confirmed. Let me verify the `INGRESS_INTERVAL_OVERLAP` constant used in the ICRC1 path before writing the final output.

Audit Report

## Title
Unbounded Ingress Window Causes OOM / Infinite Loop in ICP Rosetta `construction_payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary

The ICP Rosetta `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` values from the HTTP request metadata with no bounds check on the window size. Setting `ingress_start=0` and `ingress_end=u64::MAX` causes the internal while loop to run for approximately 153 billion iterations before exhausting all available memory, or to loop infinitely due to a truncating `as u64` cast in `Time::from_duration`. A single unauthenticated HTTP POST to `/construction/payloads` is sufficient to crash the Rosetta node.

## Finding Description

**Interval computation** (`construction_payloads.rs` L59–60):

```
interval = MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 120s = 120_000_000_000 ns
``` [1](#0-0) 

**No bounds check on caller-supplied window** (`construction_payloads.rs` L74–84): `ingress_start` and `ingress_end` are taken directly from the request metadata and converted to `Time` values with no validation of the window size. [2](#0-1) 

**Unbounded while loop** (`construction_payloads.rs` L99–107): The loop pushes one `u64` per iteration into `ingress_expiries` with no guard on the number of iterations. [3](#0-2) 

**`AddAssign<Duration>` for `Time`** (`time.rs` L55–58): Each `now += interval` calls `Time::from_duration`. [4](#0-3) 

**Truncating cast in `Time::from_duration`** (`time.rs` L103–105): `t.as_nanos()` returns `u128`; the `as u64` cast silently truncates. When `now.0` is near `u64::MAX`, the `Duration` addition succeeds (Duration stores seconds as `u64`, far exceeding `u64::MAX` nanoseconds) but the truncating cast wraps `now` back to a small value, making the loop **infinite**. [5](#0-4) 

**OOM path**: `u64::MAX / 120_000_000_000 ≈ 153 billion iterations × 8 bytes ≈ 1.2 TB`. The process OOMs and crashes well before the wrap point.

**Infinite loop path**: If OOM is somehow avoided (e.g., memory limit kills the allocator without terminating the process, or a future refactor caps allocation), the truncating cast wraps `now` back below `ingress_end`, and the loop never terminates.

**ICRC1 Rosetta variant** (`services.rs` L148–167): Has partial guards (`ingress_start >= ingress_end` and stale `ingress_end`) but no upper bound on window size. With `ingress_start = now + ingress_interval` (minimum to pass the staleness check) and `ingress_end = u64::MAX`, the step is `ingress_interval - INGRESS_INTERVAL_OVERLAP = 240s - 120s = 120s`, yielding the same ~153 billion iterations and OOM. No wrap-around issue here (plain `u64` arithmetic), but OOM is independently reachable. [6](#0-5) 

**Constants confirmed**: [7](#0-6) [8](#0-7) 

## Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with `metadata.ingress_start=0` and `metadata.ingress_end=18446744073709551615` exhausts all available memory and crashes the Rosetta node process. All ICP transfers routed through that node are blocked for the duration of the outage. This matches the allowed impact: **Application/platform-level DoS, crash** of a Rosetta node — a listed financial integration component — with concrete user harm. Severity: **High ($2,000–$10,000)**.

## Likelihood Explanation

The Rosetta HTTP API is intentionally exposed to clients. No authentication is required to call `/construction/payloads`. The exploit requires a single HTTP request with a crafted JSON body — no privileged access, no key material, no network-level attack. The only precondition is network reachability to the Rosetta port. The attack is trivially repeatable after any restart.

## Recommendation

Before entering the while loop in both the ICP and ICRC1 Rosetta handlers, validate that the ingress window does not exceed a reasonable maximum:

```rust
let max_window = ic_limits::MAX_INGRESS_TTL * 2; // or any reasonable small multiple
if ingress_end.saturating_sub(ingress_start.as_nanos_since_unix_epoch())
    > max_window.as_nanos() as u64
{
    return Err(ApiError::invalid_request(
        "ingress_end - ingress_start exceeds maximum allowed window",
    ));
}
```

Additionally, replace the truncating `as u64` cast in `Time::from_duration` (`rs/types/types/src/time.rs` L104) with a checked or saturating conversion to prevent silent wrap-around:

```rust
fn from_duration(t: Duration) -> Self {
    Time(u64::try_from(t.as_nanos()).unwrap_or(u64::MAX))
}
``` [5](#0-4) 

## Proof of Concept

```rust
#[test]
fn test_construction_payloads_oom_dos() {
    // Build a minimal RosettaRequestHandler pointed at a mock ledger.
    let handler = /* build RosettaRequestHandler */;
    let req = ConstructionPayloadsRequest {
        network_identifier: /* valid network */,
        operations: vec![/* valid TRANSACTION + FEE ops */],
        public_keys: Some(vec![/* valid public key */]),
        metadata: Some(serde_json::json!({
            "ingress_start": 0u64,
            "ingress_end": u64::MAX,
        }).as_object().cloned()),
        ..Default::default()
    };
    // Without the fix this OOMs or loops forever.
    // With the fix it must return Err within bounded time.
    let result = handler.construction_payloads(req);
    assert!(result.is_err(), "expected rejection of unbounded ingress window");
}
```

The same pattern applies to the ICRC1 variant in `rs/rosetta-api/icrc1/src/construction_api/services.rs` with `ingress_start = now + ingress_interval` (to pass the staleness check) and `ingress_end = u64::MAX`.

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

**File:** rs/types/types/src/time.rs (L103-105)
```rust
    fn from_duration(t: Duration) -> Self {
        Time(t.as_nanos() as u64)
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L148-167)
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

    // Every ingress message sent to the IC has an expiry timestamp until which the signature associated with that message is valid
    // To support a longer overall timeframe than one interval, we can send multiple ingress messages with two signable contents each
    let mut ingress_expiries = vec![];
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
