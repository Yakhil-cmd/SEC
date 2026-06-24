Audit Report

## Title
Unbounded Ingress Expiry Window Allocation in ICP Rosetta `/construction/payloads` Enables Single-Request OOM — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
The ICP Rosetta `construction_payloads` handler accepts caller-controlled `ingress_start` and `ingress_end` values deserialized directly from JSON as bare `Option<u64>` fields with no range or window-size validation. The ingress-expiry generation loop iterates `O((ingress_end − ingress_start) / interval)` times with no upper-bound guard, allowing any unprivileged HTTP caller to trigger unbounded heap allocation in a single request, causing OOM kill of the Rosetta process.

## Finding Description
The `interval` is computed at [1](#0-0)  as `MAX_INGRESS_TTL − PERMITTED_DRIFT − 120s = 300s − 60s − 120s = 120s`, confirmed by the constants at [2](#0-1) .

`ingress_start` and `ingress_end` are deserialized as bare `Option<u64>` with no range validation in `ConstructionPayloadsRequestMetadata` at [3](#0-2) , and are used without any bounds check at [4](#0-3) .

The unbounded loop at [5](#0-4)  pushes one `u64` per iteration. With `ingress_start = 0` and `ingress_end = u64::MAX ≈ 1.844 × 10¹⁹ ns`, the loop executes approximately **154 million iterations**, allocating ~1.23 GB for `ingress_expiries` alone.

`add_payloads` at [6](#0-5)  then iterates over every expiry for every transaction, cloning `HttpCanisterUpdate` and pushing two `SigningPayload` structs per expiry, multiplying total allocation by the number of operations in the request.

The ICP Rosetta path has **zero** input validation before the loop. By contrast, the ICRC1 Rosetta implementation performs partial validation at [7](#0-6)  (checking `ingress_start >= ingress_end` and `ingress_end < now + ingress_interval`), but even that does not cap the window size. The ICP path has no equivalent checks at all.

## Impact Explanation
A single malicious HTTP POST to `/construction/payloads` with crafted `ingress_start = 0` and `ingress_end = u64::MAX` causes the Rosetta process to attempt allocating multiple gigabytes of heap memory, resulting in OOM kill (complete service outage) or extreme CPU/memory pressure causing severe latency for all concurrent users. This matches the allowed High impact: **Application/platform-level DoS, crash** against the ICP Rosetta API, which is explicitly in-scope under Financial integrations. The attack is non-volumetric (tiny request body, massive amplification), making it distinct from raw volumetric DDoS.

## Likelihood Explanation
The `/construction/payloads` endpoint is unauthenticated and publicly reachable on any deployed ICP Rosetta instance. The exploit requires no privileges, no keys, and no prior state. A single HTTP request is sufficient to trigger the condition. The attack is trivially reproducible and repeatable, and the request body is minimal (two JSON integers).

## Recommendation
Add a maximum window-size guard immediately before the loop in `construction_payloads`:

```rust
let max_expiries: u64 = 100; // ~3.3 hours of coverage
let window = ingress_end
    .as_nanos_since_unix_epoch()
    .saturating_sub(ingress_start.as_nanos_since_unix_epoch());
let interval_nanos = interval.as_nanos() as u64;
if interval_nanos == 0 || window / interval_nanos > max_expiries {
    return Err(ApiError::invalid_request(
        "ingress_end − ingress_start exceeds the maximum allowed window",
    ));
}
```

Additionally, add a basic ordering check (`ingress_start >= ingress_end` → error) mirroring the ICRC1 Rosetta validation at [8](#0-7) .

## Proof of Concept
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

Expected (buggy) behavior: the loop at [5](#0-4)  executes ~154 million iterations, allocating ~1.23 GB for `ingress_expiries`, followed by `add_payloads` multiplying allocations per operation, resulting in OOM kill.

A deterministic unit test can reproduce this by calling `construction_payloads` directly with `ingress_start = Time::from_nanos_since_unix_epoch(0)` and `ingress_end = Time::from_nanos_since_unix_epoch(u64::MAX)` and asserting the process does not OOM (currently it will).

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
