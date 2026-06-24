Based on my analysis of all the relevant code, I can now provide a definitive assessment.

**Key facts established:**

1. `interval = MAX_INGRESS_TTL - PERMITTED_DRIFT - 120s = 300s - 60s - 120s = 120_000_000_000 ns`
2. `AddAssign<Duration>` for `Time` calls `Time::from_duration` which does `t.as_nanos() as u64` — a **truncating cast** with no overflow check
3. `ConstructionPayloadsRequestMetadata.ingress_start` and `ingress_end` are plain `Option<u64>` with **zero validation** in the ICP Rosetta handler
4. The ICRC1 Rosetta handler **does** have guards (`ingress_start >= ingress_end` check, `ingress_end < now + ingress_interval` check) — the ICP handler has **none**

**Overflow trace:**
- `now = Time(u64::MAX - 1)`, `ingress_end = Time(u64::MAX)`
- Iteration 1: condition true, push entry, then `now += 120_000_000_000 ns`
- `Duration::from_nanos(u64::MAX - 1) + Duration::from_nanos(120_000_000_000)` → `as_nanos()` returns `u128 = 18446744193709551614`
- `as u64` truncates to `≈ 119_999_999_998` (wraps to ~120 seconds since epoch)
- Iteration 2+: `119_999_999_998 < u64::MAX` → true → loop runs `≈ (u64::MAX - 120B) / 120B ≈ 153 billion` more iterations
- Each iteration pushes a `u64` (8 bytes) → ~1.2 exabytes of allocation → OOM crash

---

### Title
Unbounded Loop via Integer Overflow in `construction_payloads` Causes OOM DoS of ICP Rosetta Node — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

### Summary
An unprivileged caller of the ICP Rosetta API can supply crafted `ingress_start` and `ingress_end` values in the `ConstructionPayloadsRequest` metadata that trigger an integer overflow in `Time::add_assign`, causing the ingress-expiry generation loop to wrap around and execute ~153 billion iterations, exhausting process memory and crashing the Rosetta node.

### Finding Description

The `construction_payloads` handler builds a vector of ingress expiry timestamps by iterating from `ingress_start` to `ingress_end` in steps of `interval` (120 seconds in nanoseconds): [1](#0-0) 

The step `now += interval` calls `Time::add_assign`: [2](#0-1) 

which internally calls `Time::from_duration`: [3](#0-2) 

`Duration::as_nanos()` returns `u128`. The `as u64` cast **silently truncates** when the total nanoseconds exceed `u64::MAX`. With `ingress_start = u64::MAX - 1`, after one iteration `now` wraps to approximately `120_000_000_000` (120 seconds since epoch), which is far below `ingress_end = u64::MAX`, causing the loop to run ~153 billion more iterations.

The `ingress_start` and `ingress_end` fields are raw `Option<u64>` with no bounds validation in the ICP Rosetta handler: [4](#0-3) 

No guard exists before the loop — contrast with the ICRC1 Rosetta handler which explicitly rejects invalid ranges: [5](#0-4) 

The ICP handler has no equivalent check.

### Impact Explanation
The Rosetta node process allocates memory for ~153 billion `u64` entries (~1.2 exabytes), triggering an OOM kill. A single unauthenticated HTTP POST to `/construction/payloads` is sufficient to crash the node, making it unavailable for exchange operators and other integrators relying on it.

### Likelihood Explanation
The Rosetta API is a public HTTP endpoint requiring no authentication. The exploit requires only a single crafted JSON request with two specific field values. No privileged access, key material, or network-level attack is needed.

### Recommendation

1. **Add an upper bound on the ingress window** before the loop in `construction_payloads`:
   ```rust
   if ingress_end <= ingress_start {
       return Err(ApiError::invalid_request("ingress_end must be after ingress_start"));
   }
   let max_window = Duration::from_secs(24 * 3600); // e.g. 24 hours
   if ingress_end.as_nanos_since_unix_epoch()
       .saturating_sub(ingress_start.as_nanos_since_unix_epoch())
       > max_window.as_nanos() as u64
   {
       return Err(ApiError::invalid_request("ingress window too large"));
   }
   ```
2. **Replace `Time::from_duration` (truncating cast) with `Time::checked_add`** in `AddAssign` to make overflow explicit rather than silent. The safe API already exists: [6](#0-5) 
3. Mirror the validation already present in the ICRC1 Rosetta handler.

### Proof of Concept

```rust
// Single HTTP POST to /construction/payloads:
{
  "network_identifier": { ... },
  "operations": [ /* any valid transfer op */ ],
  "public_keys": [ /* any valid key */ ],
  "metadata": {
    "ingress_start": 18446744073709551614,  // u64::MAX - 1
    "ingress_end":   18446744073709551615   // u64::MAX
  }
}
// Expected: loop wraps after 1 iteration, then runs ~153 billion iterations → OOM crash.
// Assert: with the fix applied, the loop terminates after exactly 1 iteration.
```

### Citations

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

**File:** rs/rosetta-api/icp/src/models.rs (L200-223)
```rust
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
