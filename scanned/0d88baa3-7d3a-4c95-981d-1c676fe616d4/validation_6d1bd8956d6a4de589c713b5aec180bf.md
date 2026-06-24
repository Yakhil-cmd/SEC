### Title
Unbounded While-Loop DoS via Attacker-Controlled `ingress_end = u64::MAX` in `construction_payloads` — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

---

### Summary

The `construction_payloads` function in the ICRC1 Rosetta server accepts unauthenticated HTTP POST requests. When a caller supplies `metadata.ingress_end = u64::MAX` and `metadata.ingress_start = <current_time_nanos>`, both input guards pass, and the subsequent while-loop iterates ~139 million times before wrapping around in release mode — producing an infinite loop or exhausting server memory.

---

### Finding Description

The function `construction_payloads` at `services.rs:111` reads `ingress_start` and `ingress_end` directly from the unauthenticated HTTP request body: [1](#0-0) 

Two guards exist. The first rejects `ingress_start >= ingress_end`: [2](#0-1) 

The second rejects `ingress_end < now + ingress_interval`: [3](#0-2) 

Neither guard imposes an **upper bound** on `ingress_end`. With `ingress_end = u64::MAX`, both checks pass trivially. The loop then runs without any cap: [4](#0-3) 

**Concrete arithmetic:**

From `rs/limits/src/lib.rs`:
- `MAX_INGRESS_TTL = 300 s`, `PERMITTED_DRIFT = 60 s`
- `ingress_interval = 240 s = 240,000,000,000 ns` [5](#0-4) 

From `rs/rosetta-api/icrc1/src/common/constants.rs`:
- `INGRESS_INTERVAL_OVERLAP = 120 s = 120,000,000,000 ns` [6](#0-5) 

Loop step = `ingress_interval - INGRESS_INTERVAL_OVERLAP = 120,000,000,000 ns`.

With `ingress_start ≈ 1.75 × 10¹⁸ ns` (current epoch time) and `ingress_end = u64::MAX ≈ 1.844 × 10¹⁹ ns`:

```
iterations_before_overflow = (u64::MAX - now) / step
                           ≈ (1.844e19 - 1.75e18) / 1.2e11
                           ≈ 139,000,000 iterations
```

Each iteration pushes a `u64` onto `ingress_expiries`:
- Memory: `139,000,000 × 8 bytes ≈ 1.1 GB`

In **release mode** (no overflow panics), `ingress_start` wraps around after reaching `u64::MAX`, becomes a small value, and the condition `ingress_start < u64::MAX` is true again — producing an **infinite loop**. In debug mode, the `ingress_start + ingress_interval` addition at line 164 panics after OOM.

---

### Impact Explanation

An unprivileged attacker sends a single HTTP POST to `/construction/payloads` with `metadata.ingress_end = 18446744073709551615`. The Rosetta server process either:
1. Exhausts all available memory (~1.1 GB minimum) and is OOM-killed, or
2. Enters an infinite CPU loop (release build), rendering the server permanently unresponsive.

This is a complete denial-of-service of the ICRC1 Rosetta server. All users relying on the Rosetta API for transaction construction are affected until the process is manually restarted.

---

### Likelihood Explanation

- No authentication is required for `/construction/payloads` — it is a public Rosetta API endpoint by design.
- The payload is trivial to construct: a valid JSON body with `ingress_end` set to `18446744073709551615`.
- The attack is single-request, requires no prior state, and is immediately reproducible.

---

### Recommendation

Add an explicit upper-bound check on the `ingress_end - ingress_start` range before entering the loop. For example:

```rust
let max_allowed_range = ingress_interval * MAX_INGRESS_EXPIRY_COUNT; // e.g., cap at 24h
if ingress_end - ingress_start > max_allowed_range {
    return Err(Error::processing_construction_failed(
        &"Ingress window exceeds maximum allowed range"
    ));
}
```

Alternatively, cap the number of entries pushed to `ingress_expiries` with a hard limit (e.g., `MAX_INGRESS_EXPIRY_COUNT = 1440` for a 24-hour window at 1-minute steps).

---

### Proof of Concept

```rust
use std::time::SystemTime;

let now = SystemTime::now();
let result = construction_payloads(
    valid_operations(),
    Some(ConstructionPayloadsRequestMetadata {
        ingress_start: Some(
            now.duration_since(SystemTime::UNIX_EPOCH).unwrap().as_nanos() as u64
        ),
        ingress_end: Some(u64::MAX),  // attacker-controlled
        created_at_time: None,
        memo: None,
    }),
    &some_principal,
    vec![valid_public_key()],
    now,
);
// In release mode: never returns (infinite loop)
// In debug mode: OOM panic after ~139M iterations
```

### Citations

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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L148-152)
```rust
    if ingress_start >= ingress_end {
        return Err(Error::processing_construction_failed(&format!(
            "Ingress start should start before ingress end: Start: {ingress_start}, End: {ingress_end}"
        )));
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L154-158)
```rust
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

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

**File:** rs/rosetta-api/icrc1/src/common/constants.rs (L19-19)
```rust
pub const INGRESS_INTERVAL_OVERLAP: Duration = Duration::from_secs(120);
```
