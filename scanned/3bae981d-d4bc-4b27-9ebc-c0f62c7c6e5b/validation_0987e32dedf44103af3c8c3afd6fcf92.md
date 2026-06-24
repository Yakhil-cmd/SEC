### Title
Unbounded Ingress-Window Loop in ICP Rosetta `construction_payloads` Causes OOM Process Crash — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta server's `construction_payloads` handler iterates over an attacker-supplied `[ingress_start, ingress_end)` window with no upper-bound guard. Supplying `ingress_start=0` and `ingress_end=u64::MAX` causes the loop to execute ~153 million iterations (or wrap infinitely), allocating gigabytes of heap memory and crashing the Rosetta process.

---

### Finding Description

The loop at issue:

```rust
let interval =
    ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
// interval = 300s - 60s - 120s = 120 seconds = 120_000_000_000 ns

let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;
}
``` [1](#0-0) 

`ingress_start` and `ingress_end` are deserialized directly from the JSON metadata body as raw `Option<u64>` nanosecond values with no validation: [2](#0-1) 

The `Time::add_assign` implementation uses a truncating `as u64` cast on the underlying `Duration::as_nanos() -> u128`, meaning when `now` approaches `u64::MAX`, the addition wraps `now` back to a small value, making the loop **infinite** rather than merely very long: [3](#0-2) [4](#0-3) 

With `interval = 120_000_000_000 ns` and the range `[0, u64::MAX)`:
- Iterations before first wrap: `u64::MAX / 120_000_000_000 ≈ 153,722,867` iterations
- Memory allocated before wrap: `153M × 8 bytes ≈ 1.2 GB` → OOM crash
- After wrap, `now` resets to a small value still `< u64::MAX`, making the loop infinite if OOM doesn't kill the process first

**Contrast with ICRC1 Rosetta**, which has explicit guards before its equivalent loop:

```rust
if ingress_start >= ingress_end { return Err(...) }
if ingress_end < now + ingress_interval { return Err(...) }
``` [5](#0-4) 

The ICP Rosetta handler has **no equivalent guards**.

---

### Impact Explanation

An unauthenticated attacker sends a single HTTP POST to `/construction/payloads` with crafted metadata. The Rosetta server process exhausts available memory and crashes. Since Rosetta is a single-process service, this is a complete denial of service of the ICP Rosetta node. The IC protocol itself is unaffected; only the Rosetta API replica crashes.

---

### Likelihood Explanation

- No authentication is required for the Rosetta Construction API.
- The metadata fields `ingress_start` and `ingress_end` are plain JSON integers freely set by the caller.
- The exploit is a single HTTP request with two integer fields set to `0` and `18446744073709551615`.
- The `ic_limits` constants confirm `interval = 120s`, making the iteration count and memory consumption precisely calculable. [6](#0-5) 

---

### Recommendation

Add a maximum ingress-window size check before the loop, mirroring the ICRC1 Rosetta implementation:

```rust
let max_window = interval * MAX_INGRESS_EXPIRIES; // e.g., cap at 24h / interval
if ingress_end.saturating_duration_since(ingress_start) > max_window {
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

Alternatively, use `checked_add` in the loop and break/error on overflow, or cap `ingress_expiries` to a fixed maximum count (e.g., 1440 entries for a 48-hour window at 2-minute intervals).

---

### Proof of Concept

```rust
// Unit test: call construction_payloads with ingress_start=0, ingress_end=u64::MAX
// Expected: returns Err before allocating the vector
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

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```
