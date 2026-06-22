### Title
Unbounded `ingress_expiries` Allocation via Attacker-Controlled `ingress_start`/`ingress_end` Causes Rosetta Node OOM — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The `construction_payloads` handler in the ICP Rosetta node accepts attacker-controlled `ingress_start` and `ingress_end` u64 nanosecond timestamps with no server-side cap on their span. The while-loop at lines 99–107 iterates `(ingress_end - ingress_start) / interval` times, pushing one `u64` per iteration into an unbounded `Vec`. With `ingress_start=0` and `ingress_end=u64::MAX`, the loop runs ~153 billion iterations before the first arithmetic wrap, requiring ~1.2 TB of heap. The process OOMs and crashes before completing, denying service to all legitimate Rosetta users.

---

### Finding Description

**Interval computation:**

`MAX_INGRESS_TTL = 300s`, `PERMITTED_DRIFT = 60s`, so:

```
interval = 300s - 60s - 120s = 120s = 120_000_000_000 ns
``` [1](#0-0) [2](#0-1) 

**The unbounded loop:**

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;
}
``` [3](#0-2) 

With `ingress_start=0` and `ingress_end=u64::MAX`:

- Iterations before first wrap: `u64::MAX / 120_000_000_000 ≈ 153,722,867,280`
- Memory for `ingress_expiries` alone: `153.7B × 8 bytes ≈ 1.23 TB`

**Overflow behavior makes it worse — potential infinite loop:**

`Time::add` is implemented as:

```rust
fn add(self, dur: Duration) -> Time {
    Time::from_duration(Duration::from_nanos(self.0) + dur)
}
fn from_duration(t: Duration) -> Self {
    Time(t.as_nanos() as u64)  // truncating cast on overflow
}
``` [4](#0-3) [5](#0-4) 

When `now` approaches `u64::MAX`, `Duration::as_nanos()` returns a `u128` that exceeds `u64::MAX`, and the `as u64` cast **wraps** (truncates). After the wrap, `now` becomes a small value (~120s in ns), which is still `< u64::MAX`, so the loop condition remains true and the loop continues indefinitely — an **infinite loop** if the process somehow survived the OOM.

**No validation of the span exists in the ICP Rosetta handler.** The metadata fields are accepted as-is: [6](#0-5) [7](#0-6) 

Compare with the ICRC1 Rosetta handler, which at least validates `ingress_start < ingress_end` and `ingress_end >= now + interval` (though it also lacks a span cap): [8](#0-7) 

The ICP Rosetta handler has **zero** such guards.

**The `add_payloads` multiplier:** For each entry in `ingress_expiries`, `add_payloads` pushes **two** `SigningPayload` objects into `payloads` (one for the call, one for read_state), multiplying memory usage by the number of transactions: [9](#0-8) 

---

### Impact Explanation

The Rosetta node process crashes with OOM. Any exchange, wallet, or integration relying on the ICP Rosetta API for transaction construction is denied service. A single unauthenticated HTTP POST is sufficient to trigger the crash. The IC ledger itself is unaffected, but Rosetta-dependent workflows (deposits, withdrawals, balance queries via Rosetta) are interrupted.

---

### Likelihood Explanation

The Rosetta HTTP endpoint is publicly reachable by design. No authentication is required to call `/construction/payloads`. The payload is a small JSON object. A single request is sufficient to crash the process. The attack is trivially repeatable to prevent recovery.

---

### Recommendation

Add a span cap immediately before the loop:

```rust
let max_span = interval * MAX_ALLOWED_INTERVALS; // e.g., 48 * 60 * 60 seconds
if ingress_end.saturating_duration_since(ingress_start) > max_span {
    return Err(ApiError::invalid_request(
        "ingress_end - ingress_start exceeds maximum allowed span",
    ));
}
```

Or equivalently, cap `ingress_expiries.len()` to a small constant (e.g., 48 entries for a 48-hour window at 1-hour intervals) and return an error if the requested span would exceed it.

Also fix `Time::add` / `AddAssign` to use **saturating** or **checked** arithmetic instead of the truncating `as u64` cast, to eliminate the infinite-loop risk.

---

### Proof of Concept

```
POST /construction/payloads HTTP/1.1
Host: <rosetta-node>:8080
Content-Type: application/json

{
  "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
  "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"<valid_account>"},"amount":{"value":"-100000000","currency":{"symbol":"ICP","decimals":8}}}],
  "public_keys": [{"hex_bytes":"<valid_pubkey>","curve_type":"edwards25519"}],
  "metadata": {
    "ingress_start": 0,
    "ingress_end": 18446744073709551615
  }
}
```

The Rosetta process will attempt to allocate ~1.23 TB for `ingress_expiries`, exhaust available memory, and crash (SIGKILL / OOM). Repeated requests prevent restart recovery.

### Citations

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

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

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L1046-1075)
```rust
/// Add transaction and read state messages for a given update to the payloads vector.
/// Payloads are added for each ingress expiries.
fn add_payloads(
    payloads: &mut Vec<SigningPayload>,
    ingress_expiries: &[u64],
    account_identifier: &AccountIdentifier,
    update: &HttpCanisterUpdate,
    signature_type: SignatureType,
) {
    for ingress_expiry in ingress_expiries {
        let mut update = update.clone();
        update.ingress_expiry = *ingress_expiry;
        let message_id = update.id();
        let transaction_payload = SigningPayload {
            address: None,
            account_identifier: Some(account_identifier.clone()),
            hex_bytes: hex::encode(make_sig_data(&message_id)),
            signature_type: Some(signature_type),
        };
        payloads.push(transaction_payload);
        let read_state = make_read_state_from_update(&update);
        let read_state_message_id = MessageId::from(read_state.representation_independent_hash());
        let read_state_payload = SigningPayload {
            address: None,
            account_identifier: Some(account_identifier.clone()),
            hex_bytes: hex::encode(make_sig_data(&read_state_message_id)),
            signature_type: Some(signature_type),
        };
        payloads.push(read_state_payload);
    }
```

**File:** rs/types/types/src/time.rs (L48-57)
```rust
impl std::ops::Add<Duration> for Time {
    type Output = Time;
    fn add(self, dur: Duration) -> Time {
        Time::from_duration(Duration::from_nanos(self.0) + dur)
    }
}

impl std::ops::AddAssign<Duration> for Time {
    fn add_assign(&mut self, other: Duration) {
        *self = Time::from_duration(Duration::from_nanos(self.0) + other)
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
