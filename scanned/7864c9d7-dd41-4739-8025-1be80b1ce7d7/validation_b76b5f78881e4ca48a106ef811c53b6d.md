### Title
Unbounded Loop in ICP Rosetta `/construction/payloads` Allows Unprivileged DoS via Attacker-Controlled `ingress_start`/`ingress_end` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta `construction_payloads` handler iterates from `ingress_start` to `ingress_end` in fixed steps with no upper-bound guard. An unprivileged HTTP client can supply `ingress_start=0` and `ingress_end=u64::MAX` to trigger either an astronomically large loop (~153 billion iterations) or, due to a truncating overflow in `Time::from_duration`, an **infinite loop**, exhausting CPU and memory on the Rosetta node.

---

### Finding Description

**Root cause — no input validation before the loop:** [1](#0-0) 

```
interval = MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 120s = 120_000_000_000 ns
``` [2](#0-1) 

With `ingress_start = 0` and `ingress_end = u64::MAX = 18_446_744_073_709_551_615`:

```
iterations ≈ u64::MAX / 120_000_000_000 ≈ 153,722,867,280
memory     ≈ 153 billion × 8 bytes      ≈ 1.2 TB
```

**Overflow makes it infinite:** `Time::AddAssign` uses a truncating `as u64` cast: [3](#0-2) [4](#0-3) 

When `now` approaches `u64::MAX`, `Duration::from_nanos(now) + interval` produces a `Duration` whose `.as_nanos()` exceeds `u64::MAX`. The `as u64` cast silently wraps `now` back to a small value, so `now < ingress_end` (= `u64::MAX`) becomes true again — the loop never terminates.

**No guard exists in the ICP Rosetta handler.** The ICRC1 Rosetta implementation has the correct fix: [5](#0-4) 

The ICP Rosetta handler has no equivalent check. `ingress_start` and `ingress_end` are plain `Option<u64>` fields deserialized directly from the JSON body with no range validation: [6](#0-5) 

---

### Impact Explanation

An unprivileged HTTP client POSTing to `/construction/payloads` with `metadata.ingress_start=0` and `metadata.ingress_end=18446744073709551615` causes the Rosetta node process to enter an infinite loop, consuming all available CPU and triggering OOM. The Rosetta node becomes unavailable for all other clients until the process is killed and restarted. This is a constrained availability impact scoped to the Rosetta node (off-chain component).

---

### Likelihood Explanation

The endpoint is unauthenticated and publicly reachable. The payload is a standard JSON POST. No special knowledge or privilege is required. A single request is sufficient to trigger the condition. The ICRC1 Rosetta sibling already has the fix, confirming the ICP Rosetta handler is the unpatched path.

---

### Recommendation

Add the same guards present in the ICRC1 implementation before the loop in `construction_payloads`:

1. Reject if `ingress_start >= ingress_end`.
2. Reject if `ingress_end - ingress_start` exceeds a reasonable maximum (e.g., 24 hours in nanoseconds).
3. Use `Time::checked_add` instead of the plain `+=` operator to avoid the silent truncating overflow.

---

### Proof of Concept

```rust
// Unit test — no IC node required
use ic_rosetta_api::request_handler::RosettaRequestHandler;
use ic_rosetta_api::models::ConstructionPayloadsRequestMetadata;

let metadata = ConstructionPayloadsRequestMetadata {
    ingress_start: Some(0),
    ingress_end:   Some(u64::MAX),
    memo:          None,
    created_at_time: None,
};
// POST /construction/payloads with the above metadata.
// Expected: returns an error or completes in O(1).
// Actual:   enters an infinite loop; process hangs indefinitely.
```

The loop at [7](#0-6)  never terminates because `now += interval` wraps via the truncating cast in [8](#0-7) , keeping `now` permanently below `ingress_end = u64::MAX`.

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L59-107)
```rust
        let interval =
            ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);

        let meta: Option<ConstructionPayloadsRequestMetadata> = msg
            .metadata
            .as_ref()
            .map(|m| ConstructionPayloadsRequestMetadata::try_from(m.clone()))
            .transpose()
            .map_err(|e| {
                let err_msg =
                    format!("Failed to parse construction payloads request metadata: {e:?}");
                debug!("{}", err_msg);
                e
            })?;

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

        let created_at_time: ic_ledger_core::timestamp::TimeStamp = meta
            .as_ref()
            .and_then(|meta| meta.created_at_time)
            .map(ic_ledger_core::timestamp::TimeStamp::from_nanos_since_unix_epoch)
            .unwrap_or_else(|| std::time::SystemTime::now().into());

        // FIXME: the memo field needs to be associated with the operation
        let memo: Memo = meta
            .as_ref()
            .and_then(|meta| meta.memo)
            .map(Memo)
            .unwrap_or_else(|| Memo(rand::thread_rng().r#gen()));

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

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
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
