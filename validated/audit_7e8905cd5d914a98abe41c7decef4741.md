Audit Report

## Title
Unbounded `ingress_expiries` Loop via Attacker-Controlled `ingress_end` Causes OOM Process Crash — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
The ICP Rosetta `construction_payloads` handler accepts `ingress_start` and `ingress_end` directly from caller-supplied JSON metadata with no window-size validation. It then enters an unbounded `while now < ingress_end` loop, pushing one `u64` per ~120-second interval into a heap-allocated `Vec`. A single unauthenticated HTTP POST supplying `ingress_start=0` and `ingress_end=u64::MAX` forces ~153 billion iterations and ~1.2 TB of allocation, triggering OOM and process termination.

## Finding Description
The interval is fixed at compile time: [1](#0-0) 

`ingress_start` and `ingress_end` are taken verbatim from the caller's metadata with no range or window-size check: [2](#0-1) 

The loop then runs without any bound: [3](#0-2) 

With `ingress_start=0` and `ingress_end=u64::MAX`, the iteration count is `u64::MAX / (120 × 10⁹ ns) ≈ 153,722,867,280`. Each iteration pushes an 8-byte `u64`, totaling ~1.2 TB before the OOM killer terminates the process. Even a modest far-future timestamp (e.g., year 3000, ~1.57 × 10¹⁹ ns) yields ~130 billion iterations and ~1 TB.

The `ConstructionPayloadsRequestMetadata::try_from` path is pure JSON deserialization with no semantic validation: [4](#0-3) 

The ICRC1 handler has a basic ordering guard (`ingress_start >= ingress_end`) that the ICP handler entirely lacks: [5](#0-4) 

Neither handler has a maximum window-size cap, but the ICP handler is missing even the ordering guard, making it trivially exploitable with `ingress_start=0`.

## Impact Explanation
A single unauthenticated request takes the ICP Rosetta service offline until the process is manually restarted. The Rosetta API is the primary programmatic interface for exchanges and custodians integrating ICP. This matches the allowed High impact: **"Significant Rosetta/boundary/API security impact with concrete user or protocol harm"** ($2,000–$10,000). The IC consensus layer is unaffected, but the Rosetta service becomes completely unavailable.

## Likelihood Explanation
The `/construction/payloads` endpoint requires no authentication. The attack payload is a standard small JSON POST (under 1 KB). No prior knowledge beyond the public Rosetta API spec is required. The attack is a single request, requires no special tooling, and is trivially repeatable. No rate limiting or request-size guard is present in the handler path.

## Recommendation
Add a maximum ingress window size check before the loop in the ICP handler, mirroring the ICRC1 ordering guard pattern:

```rust
const MAX_INGRESS_WINDOW: Duration = Duration::from_secs(24 * 60 * 60); // 24 hours

if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request(
        "ingress_start must be before ingress_end",
    ));
}
if ingress_end > ingress_start + MAX_INGRESS_WINDOW {
    return Err(ApiError::invalid_request(
        "ingress_end exceeds maximum allowed ingress window of 24 hours",
    ));
}
```

Additionally, apply the same maximum window cap to the ICRC1 handler, which also lacks an upper bound on `ingress_end`. [6](#0-5) 

## Proof of Concept
```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [{"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"<valid_account>"},"amount":{"value":"-100000000","currency":{"symbol":"ICP","decimals":8}}}],
    "public_keys": [{"hex_bytes":"<valid_pubkey>","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
```

Expected: Rosetta process RSS grows unboundedly and is killed by the OOM killer. Computed iterations: `18446744073709551615 / 120000000000 ≈ 153,722,867,280`; each iteration allocates 8 bytes → ~1.2 TB total before termination. A unit test can confirm the bug by calling `construction_payloads` with these metadata values and asserting it returns an error rather than hanging/OOMing.

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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L148-167)
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

    // Every ingress message sent to the IC has an expiry timestamp until which the signature associated with that message is valid
    // To support a longer overall timeframe than one interval, we can send multiple ingress messages with two signable contents each
    let mut ingress_expiries = vec![];
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
```
