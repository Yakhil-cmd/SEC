Let me check the `ic_limits` constants and how `ingress_start`/`ingress_end` are parsed from the request metadata.

Now I have all the information needed for a complete analysis. Let me work through the arithmetic precisely.

**Constants:**
- `MAX_INGRESS_TTL` = 300s, `PERMITTED_DRIFT` = 60s
- `interval` = 300s − 60s − 120s = **120s = 120,000,000,000 ns**

**The overflow path in `AddAssign`:**

`AddAssign` calls `Time::from_duration(Duration::from_nanos(self.0) + other)`.

`Duration` stores seconds as `u64` (max ~584 years). `Duration::from_nanos(u64::MAX - 120e9 + 1)` ≈ 18,446,744,073 seconds — well within `u64::MAX` seconds (~18.4 quintillion). Adding 120 seconds gives ~18,446,744,193 seconds — still no `Duration` overflow, so no panic.

But `t.as_nanos()` returns a `u128`: `18,446,744,193 × 10⁹ + remainder = 18,446,744,193,589,551,616`. This exceeds `u64::MAX` (18,446,744,073,709,551,615). The `as u64` cast **silently truncates**, yielding `≈ 119,880,000,000` — about 120 seconds since the Unix epoch.

**Loop explosion:**

With `ingress_start = u64::MAX − 120e9 + 1` and `ingress_end = u64::MAX`:
- Iteration 1: `now` is near `u64::MAX`, condition true, push, then `now += interval` wraps to `≈ 120e9`
- Iteration 2+: `now ≈ 120e9 < u64::MAX` → loop runs ≈ `u64::MAX / 120e9 ≈ 153,722,867,280` more times

Each iteration pushes a `u64` (8 bytes) → **~1.2 exabytes** of allocation → OOM crash.

**No guard exists in the ICP Rosetta path** — unlike the ICRC1 version which validates `ingress_start >= ingress_end` and `ingress_end < now + ingress_interval`.

---

### Title
Unbounded `ingress_expiries` vector growth via `u64` wrap-around in `Time::AddAssign` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

### Summary

An unprivileged caller can POST a crafted `/construction/payloads` request to the ICP Rosetta node with `ingress_start` near `u64::MAX` and `ingress_end = u64::MAX`. The `now += interval` step in the ingress-expiry loop silently wraps `now` to a small value via a lossy `as u64` cast in `Time::from_duration`, causing the loop to iterate ~153 billion times and exhaust process memory.

### Finding Description

`construction_payloads` in `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs` accepts caller-controlled `ingress_start` and `ingress_end` as raw `u64` nanosecond timestamps with no range validation: [1](#0-0) 

The loop that builds `ingress_expiries` advances `now` by `interval` (120 s) each iteration: [2](#0-1) 

`Time::AddAssign` delegates to `Time::from_duration`: [3](#0-2) 

`from_duration` casts `Duration::as_nanos()` (a `u128`) to `u64` with a bare `as` — a silent truncating cast: [4](#0-3) 

`Duration` stores seconds as `u64` (max ~584 years). When `self.0 ≈ u64::MAX − 120e9`, `Duration::from_nanos(self.0) + interval` produces a `Duration` of ~18,446,744,193 seconds — valid for `Duration` but whose `as_nanos()` value exceeds `u64::MAX`. The `as u64` cast wraps `now` to ~120 seconds since the Unix epoch, and the loop then runs from that tiny value up to `ingress_end ≈ u64::MAX`, approximately **153 billion iterations**, each pushing a `u64` onto the heap.

The ICP Rosetta metadata struct accepts arbitrary `u64` values for both fields: [5](#0-4) 

The ICRC1 Rosetta counterpart has explicit guards (`ingress_start >= ingress_end` and `ingress_end < now + ingress_interval`) that the ICP Rosetta path entirely lacks: [6](#0-5) 

### Impact Explanation

The Rosetta node process exhausts virtual memory and is killed by the OS OOM killer. This takes the ICP Rosetta node offline, blocking all ICP ledger integrations (exchanges, custodians, wallets) that depend on it. A single unauthenticated HTTP POST is sufficient; no funds are at risk but service availability is fully disrupted.

### Likelihood Explanation

The `/construction/payloads` endpoint is public and unauthenticated. The payload is a simple JSON object. The trigger value (`ingress_start ≈ u64::MAX − 120e9`) is trivially computed. No special privileges, keys, or network position are required.

### Recommendation

1. Replace the bare `as u64` cast in `Time::from_duration` with a checked conversion, or use `Time::checked_add` (which already exists and is safe) in `AddAssign`: [7](#0-6) 

2. Add input validation in `construction_payloads` mirroring the ICRC1 version: reject requests where `ingress_start >= ingress_end` or where `(ingress_end − ingress_start) / interval` exceeds a small constant (e.g., 1440 for a 24-hour window).

3. Cap the `ingress_expiries` vector to a maximum size (e.g., 1440 entries) and return an error if the computed range would exceed it.

### Proof of Concept

```
POST /construction/payloads
{
  "network_identifier": { ... },
  "operations": [ <valid transfer op> ],
  "public_keys": [ <valid pk> ],
  "metadata": {
    "ingress_start": 18446744073589551616,   // u64::MAX - 120_000_000_000 + 1
    "ingress_end":   18446744073709551615    // u64::MAX
  }
}
```

After one loop iteration `now` wraps to `≈ 119,880,000,000` (120 s since epoch). The loop then runs `≈ 153,722,867,280` more iterations, each appending 8 bytes to `ingress_expiries`, allocating ~1.2 EB and crashing the process.

### Citations

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

**File:** rs/types/types/src/time.rs (L102-105)
```rust
    /// A private function to cast from [Duration] to [Time].
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
