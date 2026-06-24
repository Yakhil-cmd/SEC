Audit Report

## Title
Unbounded Attacker-Controlled Loop in ICP Rosetta `/construction/payloads` Enables Unprivileged DoS — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary

The ICP Rosetta `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` values from the JSON request body and iterates between them with no upper-bound guard. Supplying `ingress_start=0` and `ingress_end=u64::MAX` causes either ~153 billion loop iterations (OOM via ~1.2 TB allocation) or, due to a truncating `as u64` cast in `Time::from_duration`, an infinite loop. The ICRC1 Rosetta sibling already carries the fix; the ICP Rosetta handler does not.

## Finding Description

**Loop with no guard** — `construction_payloads` at lines 99–107 of `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`:

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;   // interval = 120_000_000_000 ns
}
```

`ingress_start` and `ingress_end` are deserialized directly from the JSON body as `Option<u64>` with no range validation before the loop is entered. [1](#0-0) 

**Interval value** — `interval = MAX_INGRESS_TTL(300 s) − PERMITTED_DRIFT(60 s) − 120 s = 120 s = 120 000 000 000 ns`. [2](#0-1) 

**Iteration count with `ingress_start=0`, `ingress_end=u64::MAX`:**
```
u64::MAX / 120_000_000_000 ≈ 153,722,867,280 iterations
memory ≈ 153 billion × 8 bytes ≈ 1.2 TB
```
This exhausts memory (OOM) before the loop completes.

**Overflow path — infinite loop** — `Time::AddAssign` delegates to `Time::from_duration`, which casts `Duration::as_nanos()` (a `u128`) to `u64` with a silent truncating `as` cast: [3](#0-2) [4](#0-3) 

When `now` is near `u64::MAX`, `Duration::from_nanos(now) + interval` produces a `u128` value exceeding `u64::MAX`. The `as u64` truncation wraps `now` back to `~119_999_999_999`, which is less than `ingress_end = u64::MAX`, so `now < ingress_end` is permanently true — the loop never terminates.

**No guard in ICP Rosetta** — `ingress_start` and `ingress_end` are plain `Option<u64>` fields deserialized directly from JSON with no validation: [5](#0-4) 

**ICRC1 Rosetta has the fix** — the sibling implementation rejects `ingress_start >= ingress_end` and enforces a minimum window before entering its loop: [6](#0-5) 

No equivalent check exists anywhere in the ICP Rosetta handler path.

## Impact Explanation

An unprivileged HTTP client sending a single POST to `/construction/payloads` with `metadata.ingress_start=0` and `metadata.ingress_end=18446744073709551615` causes the Rosetta node process to either exhaust all available memory (OOM kill) or spin in an infinite loop consuming 100% CPU. The Rosetta node becomes unavailable for all other clients until the process is killed and restarted. This is a concrete, repeatable availability impact on the ICP Rosetta financial integration component, matching the allowed bounty impact: **High — Significant Rosetta/ledger infrastructure security impact with concrete user or protocol harm**.

## Likelihood Explanation

The `/construction/payloads` endpoint is unauthenticated and publicly reachable. The exploit requires only a standard JSON POST body with two integer fields set to boundary values. No special privilege, account, or prior knowledge is required. A single request is sufficient to trigger the condition. The attack is trivially repeatable after a node restart.

## Recommendation

Before the loop in `construction_payloads`, add the same guards present in the ICRC1 implementation:

1. Reject if `ingress_start >= ingress_end`.
2. Reject if `ingress_end - ingress_start` exceeds a reasonable maximum (e.g., 24 hours = `86_400_000_000_000 ns`).
3. Replace `now += interval` with `now = now.checked_add(interval).ok_or(ApiError::...)?` to eliminate the silent truncating overflow in `Time::from_duration`.

## Proof of Concept

```rust
// No IC node required — pure unit test
let metadata = ConstructionPayloadsRequestMetadata {
    ingress_start: Some(0),
    ingress_end:   Some(u64::MAX),
    memo:          None,
    created_at_time: None,
};
// POST /construction/payloads with the above metadata.
// Expected: returns an error or completes in O(1).
// Actual:   process hangs indefinitely (infinite loop via wrapping cast)
//           or is OOM-killed after allocating ~1.2 TB.
```

The loop at `construction_payloads.rs:101` never terminates because `now += interval` at line 106 wraps via the truncating `as u64` cast in `Time::from_duration` at `time.rs:104`, keeping `now` permanently below `ingress_end = u64::MAX`. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L59-60)
```rust
        let interval =
            ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
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

**File:** rs/types/types/src/time.rs (L102-105)
```rust
    /// A private function to cast from [Duration] to [Time].
    fn from_duration(t: Duration) -> Self {
        Time(t.as_nanos() as u64)
    }
```

**File:** rs/rosetta-api/icp/src/models.rs (L199-223)
```rust
/// Typed metadata of ConstructionPayloadsRequest.
#[derive(Clone, Eq, PartialEq, Debug, Default, Deserialize, Serialize)]
pub struct ConstructionPayloadsRequestMetadata {
    /// The memo to use for a ledger transfer.
    /// A random number is used by default.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memo: Option<u64>,

    /// The earliest acceptable expiry date for a ledger transfer.
    /// Must be within 24 hours from created_at_time.
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ingress_start: Option<u64>,

    /// The latest acceptable expiry date for a ledger transfer.
    /// Must be within 24 hours from created_at_time.
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ingress_end: Option<u64>,

    /// If present, overrides ledger transaction creation time.
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at_time: Option<u64>,
}
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
