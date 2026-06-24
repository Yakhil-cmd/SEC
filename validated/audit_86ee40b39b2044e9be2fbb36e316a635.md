Audit Report

## Title
Unbounded `ingress_expiries` Vec Allocation via Attacker-Controlled `ingress_start`/`ingress_end` in ICP Rosetta `/construction/payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary

The ICP Rosetta `construction_payloads` handler accepts arbitrary `ingress_start` and `ingress_end` values from unauthenticated request metadata with no bounds validation before entering a `while` loop that pushes one `u64` per iteration. Setting `ingress_start=0` and `ingress_end=u64::MAX` causes ~153 billion loop iterations and ~1.23 TB of attempted heap allocation, crashing the Rosetta server process via OOM.

## Finding Description

The loop at `construction_payloads.rs` lines 99–107 iterates from `ingress_start` to `ingress_end` with step `interval = MAX_INGRESS_TTL - PERMITTED_DRIFT - 120s = 120,000,000,000 ns`: [1](#0-0) 

Both bounds are derived directly from caller-supplied metadata with no validation: [2](#0-1) 

The `ConstructionPayloadsRequestMetadata` struct accepts raw `Option<u64>` for both fields: [3](#0-2) 

The `TryFrom<ObjectMap>` implementation performs only a plain `serde_json::from_value` with no range checks: [4](#0-3) 

The `interval` is computed at line 59–60: [5](#0-4) 

With `ingress_start=0` and `ingress_end=18446744073709551615` (`u64::MAX`), the loop runs `u64::MAX / 120_000_000_000 ≈ 153,722,867,280` iterations, each pushing 8 bytes, totaling ~1.23 TB of attempted allocation.

The ICRC1 Rosetta sibling already contains the correct guards before its equivalent loop: [6](#0-5) 

The ICP Rosetta handler has no equivalent guards.

## Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with the malicious metadata causes the Rosetta server process to exhaust heap memory and crash (OOM kill or allocation panic). This takes down the ICP Rosetta node entirely, denying service to all users relying on it for ledger interaction (exchanges, wallets, custodians). This matches the allowed impact: **High ($2,000–$10,000) — Application/platform-level DoS of a Rosetta/financial integration component with concrete user and protocol harm**, as well as **Significant Rosetta/ledger infrastructure security impact with concrete user harm**.

## Likelihood Explanation

The `/construction/payloads` endpoint is publicly reachable with no authentication. The exploit payload is a small JSON object. A single request is sufficient to trigger the crash. The attack is trivially repeatable — the server can be crashed again immediately after restart. No special privileges, victim interaction, or network-level attack is required.

## Recommendation

Add a window-size cap before the loop, mirroring the ICRC1 Rosetta pattern at `rs/rosetta-api/icrc1/src/construction_api/services.rs` lines 148–158:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request(
        "ingress_start must be before ingress_end",
    ));
}
let max_window = interval * 100; // e.g., cap at 100 intervals (~3.3 hours)
if ingress_end
    .as_nanos_since_unix_epoch()
    .saturating_sub(ingress_start.as_nanos_since_unix_epoch())
    > max_window.as_nanos() as u64
{
    return Err(ApiError::invalid_request("ingress window too large"));
}
```

## Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"<valid_address>"},"amount":{"value":"-1","currency":{"symbol":"ICP","decimals":8}}}],
    "public_keys": [{"hex_bytes":"<valid_ed25519_pubkey>","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
```

The server process will exhaust heap memory and crash. A deterministic unit test can also be written by calling `construction_payloads` directly with `ingress_start=0` and `ingress_end=u64::MAX` and asserting it returns an error rather than hanging/OOMing.

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

**File:** rs/rosetta-api/icp/src/models.rs (L240-249)
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
