Audit Report

## Title
Unbounded `ingress_expiries` Vec allocation via uncapped `ingress_end` in `construction_payloads` — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

## Summary

The `construction_payloads` function in the ICRC1 Rosetta node accepts attacker-controlled `ingress_end` values with no upper-bound validation. Submitting `ingress_start=0, ingress_end=u64::MAX` bypasses both existing guards and causes the while loop to iterate approximately 153 million times, pushing one `u64` per iteration into an unbounded `Vec`, exhausting process heap memory and crashing the Rosetta node.

## Finding Description

**Constants confirmed from source:**
- `MAX_INGRESS_TTL` = 300s, `PERMITTED_DRIFT` = 60s → `ingress_interval` = 240s = 2.4×10¹¹ ns
- `INGRESS_INTERVAL_OVERLAP` = 120s = 1.2×10¹¹ ns
- Loop step = `ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP)` = 1.2×10¹¹ ns

**Exploit path:**

1. `ConstructionPayloadsRequestMetadata.ingress_end` is a plain `Option<u64>` with no range constraint at the type level.

2. Guard at line 148: `ingress_start(0) >= ingress_end(u64::MAX)` → `FALSE` → no error.

3. Guard at line 154: `ingress_end < now + ingress_interval` → `u64::MAX < ~1.76×10¹⁸` → `FALSE` → no error. This check is a lower-bound only (ensuring `ingress_end` is sufficiently in the future); it provides no upper-bound protection.

4. The while loop at line 163 runs with `ingress_start` advancing from 0 in steps of 1.2×10¹¹ until it reaches `u64::MAX`:
   - Iterations ≈ 1.844×10¹⁹ / 1.2×10¹¹ ≈ **153,722,867**
   - Each iteration pushes one `u64` (8 bytes) onto `ingress_expiries`
   - Total Vec allocation ≈ **~1.23 GB** before the loop terminates (or OOM kills the process first)

5. After the loop, `handle_construction_payloads` would iterate over `ingress_expiries` and clone `canister_method_args` for each entry, compounding memory usage further — but OOM occurs during the loop itself.

No authentication, no special privileges, and no large request body are required. The entire attack payload is a small JSON object.

## Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with `ingress_end` set to `u64::MAX` causes the ICRC1 Rosetta process to allocate ~1.23 GB in a tight loop and crash with OOM. The process does not recover without a restart. This is a complete, repeatable denial-of-service against the ICRC1 Rosetta node, which is an in-scope financial integration component. This matches the **High ($2,000–$10,000)** impact category: "Application/platform-level DoS, crash, or availability impact not based on raw volumetric DDoS" and "Significant Rosetta security impact with concrete user or protocol harm."

## Likelihood Explanation

The `/construction/payloads` endpoint is a standard public Rosetta API endpoint registered without authentication at line 380 of `rs/rosetta-api/icrc1/src/main.rs`. No rate limiting or body-size guard on the server-side loop is visible in the ICRC1 Rosetta Axum router. Any client that can reach the HTTP port can trigger this with a single request. The attack is trivially repeatable after each restart.

## Recommendation

Add an explicit upper-bound check on `ingress_end` before the while loop. For example, cap the allowed window to a reasonable maximum (e.g., 24 hours):

```rust
const MAX_INGRESS_WINDOW_NS: u64 = 24 * 60 * 60 * 1_000_000_000u64;
if ingress_end > now + MAX_INGRESS_WINDOW_NS {
    return Err(Error::processing_construction_failed(&format!(
        "ingress_end {ingress_end} exceeds maximum allowed window from now"
    )));
}
```

Alternatively, cap the number of iterations directly:

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
fn test_construction_payloads_unbounded_allocation() {
    use std::time::{Duration, SystemTime, UNIX_EPOCH};
    // Simulate server time in 2026 (~1.75e18 ns since epoch)
    let now = UNIX_EPOCH + Duration::from_secs(1_750_000_000);
    // ingress_start=0 passes guard 1 (0 < u64::MAX)
    // ingress_end=u64::MAX passes guard 2 (u64::MAX is NOT < now+interval)
    // The while loop then runs ~153M iterations → OOM before returning
    let result = construction_payloads(
        vec![/* valid transfer operation */],
        Some(ConstructionPayloadsRequestMetadata {
            ingress_start: Some(0),
            ingress_end: Some(u64::MAX),
            created_at_time: None,
            memo: None,
        }),
        &Principal::anonymous(),
        vec![/* valid public key */],
        now,
    );
    // Expected: Err(...), Actual: process OOM-crashes before returning
    assert!(result.is_err());
}
```

The function will not return an error — it will exhaust available memory during the loop at line 163 before completing.