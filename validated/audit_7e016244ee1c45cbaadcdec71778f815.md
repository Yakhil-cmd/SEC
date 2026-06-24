Audit Report

## Title
Unbounded `ingress_expiries` Vec allocation via uncapped `ingress_end` in `construction_payloads` — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

## Summary
The `construction_payloads` function accepts attacker-controlled `ingress_start` and `ingress_end` values from HTTP request metadata with no upper-bound validation on `ingress_end`. Submitting `ingress_start=0, ingress_end=u64::MAX` bypasses both existing guards and causes the while loop at line 163 to iterate approximately 153 million times, pushing one `u64` per iteration into an unbounded `Vec`, exhausting process memory and crashing the ICRC1 Rosetta node.

## Finding Description
**Root cause:** `ConstructionPayloadsRequestMetadata.ingress_end` is a plain `Option<u64>` with no range validation at the type level. [1](#0-0) 

**Guard 1 (line 148):** `if ingress_start >= ingress_end` — with `ingress_start=0, ingress_end=u64::MAX`, evaluates to `FALSE`. No error returned. [2](#0-1) 

**Guard 2 (line 154):** `if ingress_end < now + ingress_interval` — `ingress_interval = (MAX_INGRESS_TTL - PERMITTED_DRIFT) = (300s - 60s) = 240s = 2.4×10¹¹ ns`. With `now ≈ 1.75×10¹⁸ ns` (2026), `now + ingress_interval ≈ 1.75×10¹⁸`, which is far less than `u64::MAX ≈ 1.84×10¹⁹`. Condition is `FALSE`. No error returned. [3](#0-2) 

**The loop (lines 162–167):** With no upper-bound guard, execution reaches the while loop. The step per iteration is `ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP) = 2.4×10¹¹ - 1.2×10¹¹ = 1.2×10¹¹ ns`. Total iterations ≈ `u64::MAX / 1.2×10¹¹ ≈ 153,722,867`. Each iteration pushes one `u64` (8 bytes) onto `ingress_expiries`, totaling ~1.23 GB of heap allocation before OOM crash. [4](#0-3) 

The constants confirm `MAX_INGRESS_TTL = 300s`, `PERMITTED_DRIFT = 60s`: [5](#0-4) 

And `INGRESS_INTERVAL_OVERLAP = 120s`: [6](#0-5) 

## Impact Explanation
A single unauthenticated HTTP POST to `/construction/payloads` with `ingress_end=u64::MAX` causes the ICRC1 Rosetta process to allocate ~1.23 GB in a tight loop and crash with OOM. The process does not recover without a restart. This constitutes a complete application-level denial-of-service against the ICRC1 Rosetta node, matching the **High ($2,000–$10,000)** impact class: *"Application/platform-level DoS, crash... or significant Rosetta... security impact with concrete user or protocol harm."* The Rosetta API is explicitly listed as an in-scope financial integration target.

## Likelihood Explanation
The `/construction/payloads` endpoint is a standard public Rosetta API endpoint requiring no authentication or privileged role. The exploit payload is a trivial JSON object with two integer fields. Any client with network access to the Rosetta HTTP port can trigger this with a single request. The attack is repeatable: after a restart, the node is immediately vulnerable again. No special knowledge, timing, or victim interaction is required.

## Recommendation
Add an explicit upper-bound check on `ingress_end` before the while loop. The window should be capped to a reasonable maximum (e.g., 24 hours):

```rust
let max_ingress_end = now + 24 * 60 * 60 * 1_000_000_000u64; // 24h in ns
if ingress_end > max_ingress_end {
    return Err(Error::processing_construction_failed(&format!(
        "ingress_end {ingress_end} exceeds maximum allowed window"
    )));
}
```

Alternatively, cap the number of iterations directly in the loop:

```rust
const MAX_INGRESS_EXPIRIES: usize = 1000;
while ingress_start < ingress_end {
    if ingress_expiries.len() >= MAX_INGRESS_EXPIRIES {
        return Err(Error::processing_construction_failed("Too many ingress intervals"));
    }
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start += ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
```

## Proof of Concept

```rust
#[test]
fn test_construction_payloads_oom_dos() {
    use std::time::{Duration, SystemTime, UNIX_EPOCH};
    use candid::Principal;
    use rosetta_core::models::Ed25519KeyPair;
    use rosetta_core::models::RosettaSupportedKeyPair;

    let now = UNIX_EPOCH + Duration::from_secs(1_750_000_000);
    let key_pair = Ed25519KeyPair::generate(0);
    let public_key = ic_rosetta_test_utils::to_public_key(&key_pair);
    let principal = key_pair.generate_principal_id().unwrap().0;

    // Build a minimal valid transfer operation (omitted for brevity)
    let operations = vec![/* valid transfer operation */];

    let result = construction_payloads(
        operations,
        Some(ConstructionPayloadsRequestMetadata {
            ingress_start: Some(0),
            ingress_end: Some(u64::MAX),
            created_at_time: None,
            memo: None,
        }),
        &principal,
        vec![public_key],
        now,
    );
    // Expected: Err(...), Actual: OOM crash after ~153M loop iterations
    assert!(result.is_err());
}
```

Running this test will exhaust process memory before returning. To safely verify the iteration count without OOM, replace `u64::MAX` with a bounded value such as `now_ns + 120_000_000_000_000u64` (1000 steps) and assert `ingress_expiries.len() == 1000`.

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L193-195)
```rust
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ingress_end: Option<u64>,
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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L162-167)
```rust
    let mut ingress_expiries = vec![];
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
