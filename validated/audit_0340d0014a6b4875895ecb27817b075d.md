### Title
Integer Overflow in `construction_payloads` Causes Unbounded Loop / DoS - (File: `rs/rosetta-api/icrc1/src/construction_api/services.rs`)

### Summary
The ICRC1 Rosetta API's `construction_payloads` function performs raw `u64` arithmetic on user-controlled `ingress_start` and `ingress_end` values without overflow guards. When an attacker supplies values near `u64::MAX`, the while-loop that builds `ingress_expiries` overflows and wraps, causing `ingress_start` to reset to a tiny value that is permanently less than `ingress_end`, producing an infinite loop that exhausts CPU and memory.

### Finding Description
In `rs/rosetta-api/icrc1/src/construction_api/services.rs`, the function `construction_payloads` accepts user-supplied `ingress_start` and `ingress_end` as raw `u64` nanosecond timestamps: [1](#0-0) 

Two validation checks exist, but neither prevents the overflow: [2](#0-1) 

The loop that follows uses unchecked `u64` addition: [3](#0-2) 

In Rust release builds, `u64` overflow wraps silently. When `ingress_start ≈ u64::MAX - 1` and `ingress_end = u64::MAX`:

1. **Check 1** (`ingress_start >= ingress_end`): `u64::MAX - 1 >= u64::MAX` → **false** → passes.
2. **Check 2** (`ingress_end < now + ingress_interval`): `u64::MAX < ~1.7×10¹⁸ + 2.4×10¹¹` → **false** → passes (current epoch time is ~1.7×10¹⁸ ns, far below `u64::MAX ≈ 1.8×10¹⁹`).
3. **Loop body**: `ingress_start + ingress_interval` wraps to a small value and is pushed to `ingress_expiries`. Then `ingress_start += (ingress_interval - INGRESS_INTERVAL_OVERLAP)` also wraps to a small value.
4. **Loop condition**: small value `< u64::MAX` → **true** → loop repeats forever.

The ICP Rosetta API avoids this because it uses `ic_types::time::Time`, whose `Add` implementation uses `saturating_add`: [4](#0-3) 

The ICRC1 path has no equivalent protection.

The `ingress_interval` constant is computed without overflow protection either: [5](#0-4) 

### Impact Explanation
An attacker sends a single crafted HTTP POST to `/construction/payloads` on the ICRC1 Rosetta node. The handler thread enters an infinite loop, consuming 100% CPU and unbounded memory (the `ingress_expiries` vector grows without bound). The Rosetta API process becomes unresponsive, blocking all ICRC1 token transfer operations routed through it. This is a **boundary/API validation bypass** causing a complete denial-of-service of the ICRC1 Rosetta financial integration layer.

### Likelihood Explanation
The `/construction/payloads` endpoint is unauthenticated and publicly reachable. The attack requires a single HTTP request with two crafted integer fields. No special privileges, keys, or prior knowledge are needed. Any external party aware of the Rosetta API can trigger this.

### Recommendation
Replace raw `u64` arithmetic with checked or saturating operations, and add an explicit upper-bound validation on `ingress_start` and `ingress_end` before entering the loop, mirroring the safe pattern used in the ICP Rosetta handler:

```rust
// Validate inputs are within representable range
if ingress_start > u64::MAX - ingress_interval {
    return Err(Error::processing_construction_failed(
        "ingress_start too large: would overflow when adding ingress_interval",
    ));
}
// Use checked addition inside the loop
while ingress_start < ingress_end {
    let expiry = ingress_start.checked_add(ingress_interval)
        .ok_or_else(|| Error::processing_construction_failed("ingress expiry overflow"))?;
    ingress_expiries.push(expiry);
    ingress_start = ingress_start.saturating_add(
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64)
    );
}
```

### Proof of Concept

Send the following HTTP request to the ICRC1 Rosetta API:

```http
POST /construction/payloads HTTP/1.1
Content-Type: application/json

{
  "network_identifier": { ... },
  "operations": [ /* valid ICRC1 transfer operation */ ],
  "public_keys": [ /* valid public key */ ],
  "metadata": {
    "ingress_start": 18446744073709551614,
    "ingress_end":   18446744073709551615
  }
}
```

- `ingress_start = u64::MAX - 1 = 18446744073709551614`
- `ingress_end   = u64::MAX     = 18446744073709551615`

Both validation checks pass. The while loop at [3](#0-2) 

immediately overflows `ingress_start` to a small value on the first iteration, then loops forever. The Rosetta API process hangs indefinitely, denying service to all subsequent callers.

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L120-121)
```rust
    let ingress_interval: u64 =
        (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos() as u64;
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L128-136)
```rust
    let mut ingress_start = metadata
        .as_ref()
        .and_then(|meta| meta.ingress_start)
        .unwrap_or(now);

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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L163-167)
```rust
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
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
