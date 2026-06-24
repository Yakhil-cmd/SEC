### Title
Unbounded Loop via Attacker-Controlled `ingress_start`/`ingress_end` in ICP Rosetta `/construction/payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

### Summary

The ICP Rosetta `/construction/payloads` handler accepts user-supplied `ingress_start` and `ingress_end` values with no bounds validation, then iterates a `while now < ingress_end` loop that pushes one entry per interval. With `ingress_start=0` and `ingress_end=u64::MAX`, the loop first runs ~153 billion iterations (OOM), and then — due to a truncating `as u64` cast in `Time::from_duration` — wraps `now` back to a small value and becomes **infinite**.

---

### Finding Description

**The loop** at [1](#0-0) :

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    let ingress_expiry = (now + ...).as_nanos_since_unix_epoch();
    ingress_expiries.push(ingress_expiry);
    now += interval;
}
```

**The interval** is computed as: [2](#0-1) 

```
MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 120s = 120,000,000,000 ns
```

Confirmed from: [3](#0-2) 

**The inputs are fully attacker-controlled** — `ingress_start` and `ingress_end` are `Option<u64>` fields deserialized directly from JSON with no range validation: [4](#0-3) 

**No guard exists** in the ICP Rosetta handler. Compare to the ICRC1 Rosetta handler, which explicitly rejects bad ranges: [5](#0-4) 

The ICP handler has no equivalent check.

**The wrap-around infinite loop** is caused by `Time::AddAssign`: [6](#0-5) 

which calls: [7](#0-6) 

```rust
fn from_duration(t: Duration) -> Self {
    Time(t.as_nanos() as u64)  // truncating cast: u128 → u64
}
```

`t.as_nanos()` returns `u128`. When `now` exceeds `u64::MAX - 120_000_000_000`, the resulting `u128` nanos value exceeds `u64::MAX`, and the `as u64` cast **silently wraps** `now` back to a small value. Since `ingress_end = u64::MAX`, the condition `now < ingress_end` is immediately true again — producing an **infinite loop**.

---

### Impact Explanation

- **Phase 1 (OOM):** `u64::MAX / 120_000_000_000 ≈ 153,722,867,280` iterations, each pushing a `u64` onto `ingress_expiries`. That is ~1.2 TB of heap allocation, exhausting memory on any real host.
- **Phase 2 (infinite loop):** If OOM does not kill the process first, the `as u64` wrap causes `now` to reset to a small value, making the loop infinite and consuming 100% of one CPU core indefinitely.
- Either outcome renders the Rosetta node completely unavailable.

---

### Likelihood Explanation

The endpoint is unauthenticated HTTP. Any client can POST to `/construction/payloads` with `metadata.ingress_start=0` and `metadata.ingress_end=18446744073709551615`. No credentials, no privileged role, no network position required. A single request is sufficient to trigger the condition.

---

### Recommendation

Add an explicit upper-bound check on the ingress window before the loop, mirroring the ICRC1 Rosetta implementation:

```rust
let max_window = Duration::from_secs(24 * 3600); // or some reasonable cap
if ingress_end.saturating_duration_since(ingress_start) > max_window {
    return Err(ApiError::invalid_request(
        "ingress_end - ingress_start exceeds maximum allowed window"
    ));
}
```

Additionally, replace the unchecked `as u64` cast in `Time::from_duration` with a checked or saturating conversion to prevent silent wrap-around.

---

### Proof of Concept

```
POST /construction/payloads HTTP/1.1
Content-Type: application/json

{
  "network_identifier": { "blockchain": "Internet Computer", "network": "<ledger_canister_id>" },
  "operations": [/* any valid transfer operation */],
  "public_keys": [/* any valid public key */],
  "metadata": {
    "ingress_start": 0,
    "ingress_end": 18446744073709551615
  }
}
```

The server thread handling this request enters an unbounded loop, allocating memory until OOM or looping infinitely after wrap-around. The Rosetta node becomes unresponsive to all subsequent requests.

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

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

**File:** rs/rosetta-api/icp/src/models.rs (L201-223)
```rust
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
