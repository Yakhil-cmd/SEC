I now have all the information needed to make a determination. Let me analyze the key facts:

**Interval calculation:**
- `MAX_INGRESS_TTL = 300s`, `PERMITTED_DRIFT = 60s`, extra `120s` subtracted
- `interval = 300s - 60s - 120s = 120s = 120_000_000_000 ns`

**`Time::AddAssign` overflow behavior** (lines 55-59 of `rs/types/types/src/time.rs`):
```rust
impl std::ops::AddAssign<Duration> for Time {
    fn add_assign(&mut self, other: Duration) {
        *self = Time::from_duration(Duration::from_nanos(self.0) + other)
    }
}
// from_duration:
fn from_duration(t: Duration) -> Self {
    Time(t.as_nanos() as u64)  // truncating cast, not saturating
}
```

The `as u64` cast on a `u128` **truncates** (wraps), it does not saturate. This means when `now` approaches `u64::MAX` and `interval` is added, the result wraps back to a small value — still less than `u64::MAX` — making the loop **infinite**, not merely very long.

**No guards in ICP Rosetta** (lines 99-107 of `construction_payloads.rs`) vs. ICRC1 Rosetta which explicitly rejects `ingress_start >= ingress_end` and enforces a minimum `ingress_end` (lines 148-158 of `rs/rosetta-api/icrc1/src/construction_api/services.rs`).

---

### Title
Unbounded ingress-expiry loop in ICP Rosetta `/construction/payloads` enables OOM/infinite-loop DoS — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

### Summary
The `construction_payloads` function in ICP Rosetta accepts attacker-controlled `ingress_start` and `ingress_end` timestamps with no upper-bound validation. Setting `ingress_start=0` and `ingress_end=u64::MAX` causes the ingress-expiry accumulation loop to run effectively forever (due to a truncating `as u64` overflow in `Time::AddAssign`), OOM-killing or permanently hanging the Rosetta process.

### Finding Description
In `construction_payloads` [1](#0-0) , the loop:

```rust
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;   // interval = 120 seconds
}
```

has no guard on the range `ingress_end - ingress_start`. The `interval` is 120 seconds (120,000,000,000 ns). [2](#0-1) 

With `ingress_start=0` and `ingress_end=u64::MAX`, the loop would need ~153 billion iterations before `now` reaches `u64::MAX`. Each iteration pushes a `u64` (8 bytes) to `ingress_expiries`, requiring ~1.2 TB of heap — an OOM crash long before completion.

Worse, `Time::AddAssign` uses a **truncating** `as u64` cast on the `u128` result of `Duration::as_nanos()`: [3](#0-2) 

```rust
fn add_assign(&mut self, other: Duration) {
    *self = Time::from_duration(Duration::from_nanos(self.0) + other)
    // from_duration: Time(t.as_nanos() as u64)  ← truncating, not saturating
}
``` [4](#0-3) 

When `now` overflows `u64::MAX`, it wraps back to a small value still less than `u64::MAX`, making the loop **infinite**. The process never returns from the request handler.

The ICRC1 Rosetta counterpart explicitly prevents this with two guards: [5](#0-4) 

```rust
if ingress_start >= ingress_end { return Err(...); }
if ingress_end < now + ingress_interval { return Err(...); }
```

Neither guard exists in the ICP Rosetta path.

### Impact Explanation
A single unauthenticated HTTP POST to `/construction/payloads` with `metadata.ingress_start=0` and `metadata.ingress_end=18446744073709551615` causes the ICP Rosetta process to either OOM-crash (before wrap-around) or spin in an infinite loop (after wrap-around). Either outcome renders the Rosetta node completely unavailable. Since Rosetta is a stateless HTTP service, the attack can be repeated immediately after any restart.

### Likelihood Explanation
The endpoint is publicly accessible with no authentication. The malicious payload is a trivially crafted JSON body. The ICRC1 Rosetta fix demonstrates the developers are aware of the need for this guard — it was simply not applied to the ICP Rosetta path.

### Recommendation
Add the same guards present in the ICRC1 counterpart before the loop in `construction_payloads`:
1. Reject if `ingress_start >= ingress_end`.
2. Enforce a maximum window, e.g., `ingress_end - ingress_start <= some_reasonable_cap` (e.g., 24 hours = 86,400 intervals maximum).
3. Replace the truncating `as u64` cast in `Time::from_duration` with a saturating or checked conversion to eliminate the infinite-loop path.

### Proof of Concept
```
POST /construction/payloads HTTP/1.1
Content-Type: application/json

{
  "network_identifier": { ... },
  "operations": [ <any valid transfer op> ],
  "public_keys": [ <any valid key> ],
  "metadata": {
    "ingress_start": 0,
    "ingress_end": 18446744073709551615
  }
}
```
The Rosetta process will either exhaust memory (OOM kill) or spin infinitely, causing sustained unavailability. A unit test asserting the function returns an error or completes in bounded time with these inputs will reproduce the issue deterministically.

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
