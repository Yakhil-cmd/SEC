The code is clear. Let me verify the `ic_limits` constants to confirm the exact iteration count.

### Title
Unbounded Ingress Expiry Window Allocation in ICP Rosetta `/construction/payloads` Enables Single-Request OOM — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta `construction_payloads` handler accepts caller-controlled `ingress_start` and `ingress_end` values with no bounds validation. The ingress-expiry generation loop iterates `O((ingress_end − ingress_start) / interval)` times with no cap, allowing any unprivileged HTTP caller to trigger unbounded heap allocation in a single request.

---

### Finding Description

In `construction_payloads`, the `interval` is computed as:

```
interval = MAX_INGRESS_TTL − PERMITTED_DRIFT − 120s
         = 300s − 60s − 120s = 120s = 1.2 × 10¹¹ ns
``` [1](#0-0) [2](#0-1) 

`ingress_start` and `ingress_end` are deserialized directly from the caller-supplied JSON metadata as bare `Option<u64>` fields with no range or window-size validation: [3](#0-2) 

The loop that builds `ingress_expiries` then runs without any upper-bound guard:

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;
}
``` [4](#0-3) 

With `ingress_start = 0` and `ingress_end = u64::MAX ≈ 1.844 × 10¹⁹ ns`, the loop executes approximately **154 million iterations**, pushing a `u64` each time (~1.23 GB for `ingress_expiries` alone). `add_payloads` then iterates over every expiry for every transaction, cloning `HttpCanisterUpdate` and pushing two `SigningPayload` structs per expiry: [5](#0-4) 

The total allocation grows proportionally to the number of operations in the request multiplied by the window size.

**No equivalent guard exists in the ICP Rosetta path.** By contrast, the ICRC1 Rosetta implementation does perform partial validation (checking `ingress_start >= ingress_end` and `ingress_end < now + interval`), but even that does not cap the window size: [6](#0-5) 

The ICP Rosetta path has **zero** such checks before the loop.

---

### Impact Explanation

A single malicious HTTP POST to `/construction/payloads` with a crafted metadata object causes the Rosetta process to attempt allocating multiple gigabytes of heap memory. This results in either:
- **OOM kill** of the Rosetta process (complete service outage), or
- **Extreme CPU/memory pressure** causing severe latency for all concurrent users.

Because the request body is tiny (two JSON integers), this is a non-volumetric, amplification-style availability attack against the Rosetta API server. Exchanges and integrators relying on ICP Rosetta for transaction construction would be directly affected.

---

### Likelihood Explanation

The `/construction/payloads` endpoint is unauthenticated and publicly reachable on any deployed ICP Rosetta instance. The exploit requires no privileges, no keys, and no prior state. A single HTTP request is sufficient to trigger the condition. The attack is trivially reproducible locally.

---

### Recommendation

Add a maximum window-size guard immediately before the loop:

```rust
let max_expiries: u64 = 100; // e.g., ~3.3 hours of coverage
let window = ingress_end.as_nanos_since_unix_epoch()
    .saturating_sub(ingress_start.as_nanos_since_unix_epoch());
let interval_nanos = interval.as_nanos() as u64;
if window / interval_nanos > max_expiries {
    return Err(ApiError::invalid_request(
        "ingress_end − ingress_start exceeds the maximum allowed window",
    ));
}
```

This mirrors the intent of the ICRC1 Rosetta validation and should be applied before any allocation occurs.

---

### Proof of Concept

```
POST /construction/payloads
Content-Type: application/json

{
  "network_identifier": { "blockchain": "Internet Computer", "network": "<ledger_canister_id>" },
  "operations": [{ "operation_identifier": {"index": 0}, "type": "TRANSACTION", ... }],
  "public_keys": [{ "hex_bytes": "<valid_pk>", "curve_type": "edwards25519" }],
  "metadata": {
    "ingress_start": 0,
    "ingress_end": 18446744073709551615
  }
}
```

Expected (buggy) behavior: Rosetta allocates ~1.23 GB for `ingress_expiries` then OOMs.
Expected (fixed) behavior: `ApiError` returned immediately with "window too large". [7](#0-6)

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

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L1048-1076)
```rust
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
