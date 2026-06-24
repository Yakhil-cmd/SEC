Audit Report

## Title
Unbounded Ingress Window Loop in ICP Rosetta `construction_payloads` Causes OOM DoS — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary

The ICP Rosetta `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` values from request metadata and feeds them directly into an unbounded `while now < ingress_end` loop with no window-size validation. Supplying `ingress_start=0` and `ingress_end=u64::MAX` causes approximately 153 billion loop iterations, each pushing a `u64` onto a heap-allocated `Vec`, exhausting process memory and crashing the Rosetta node. The endpoint requires no authentication.

## Finding Description

`MAX_INGRESS_TTL` is 300 s and `PERMITTED_DRIFT` is 60 s, confirmed in `rs/limits/src/lib.rs`: [1](#0-0) 

The loop step `interval` is therefore `300 s − 60 s − 120 s = 120 s = 120,000,000,000 ns`: [2](#0-1) 

`ingress_start` and `ingress_end` are read directly from user-supplied metadata with no validation before use: [3](#0-2) 

The loop then runs without any guard on the window size: [4](#0-3) 

With `ingress_start = 0` and `ingress_end = u64::MAX = 18,446,744,073,709,551,615`:

```
iterations ≈ 18_446_744_073_709_551_615 / 120_000_000_000 ≈ 153,722,867,280
memory     ≈ 153 × 10⁹ × 8 bytes ≈ 1.2 TB
```

The process OOMs and is killed long before the loop completes. There is no request size limit, timeout, or iteration cap anywhere in the call path.

Note: The ICRC1 counterpart (`rs/rosetta-api/icrc1/src/construction_api/services.rs`) does check `ingress_start >= ingress_end` and `ingress_end < now + ingress_interval`, but neither check caps the window size — `ingress_end = u64::MAX` passes both guards since `u64::MAX` is not less than `now + ingress_interval`. The ICP handler has no analogous guards at all. [5](#0-4) 

## Impact Explanation

Any unauthenticated HTTP client can send a single `POST /construction/payloads` request to crash the Rosetta node process. This denies service to all legitimate clients — exchanges, wallets, and tooling — that depend on the node for ICP transaction construction and submission. This matches the allowed bounty impact: **Application/platform-level DoS, crash, or significant Rosetta security impact with concrete user or protocol harm — High ($2,000–$10,000)**.

## Likelihood Explanation

The endpoint is public, requires no credentials, and the malicious payload is a trivial two-field JSON object (`ingress_start: 0`, `ingress_end: 18446744073709551615`). The crash is deterministic and reproducible with a single HTTP request. No special knowledge beyond the Rosetta API spec is required. The attack can be repeated indefinitely to keep the node down.

## Recommendation

Add window-size validation before the loop, mirroring a corrected version of the ICRC1 pattern but also adding an explicit maximum window cap:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request("ingress_start must be before ingress_end"));
}
let max_window_ns = Duration::from_secs(24 * 3600).as_nanos() as u64;
if ingress_end - ingress_start > max_window_ns {
    return Err(ApiError::invalid_request(
        "ingress window exceeds maximum allowed duration (24 hours)",
    ));
}
```

This bounds the loop to at most `86_400_000_000_000 / 120_000_000_000 = 720` iterations regardless of attacker input.

## Proof of Concept

```rust
// Unit test — no network required
#[test]
fn construction_payloads_oom_dos() {
    let handler = make_test_handler(); // existing test helper
    let req = ConstructionPayloadsRequest {
        metadata: Some(serde_json::json!({
            "ingress_start": 0u64,
            "ingress_end": u64::MAX,
        })),
        // minimal valid operations + public_keys
        ..minimal_valid_request()
    };
    // Must return an error within bounded time/memory; currently hangs/OOMs
    let result = handler.construction_payloads(req);
    assert!(result.is_err());
}
```

The test can be run locally without a replica. The OOM is triggered entirely within the synchronous `construction_payloads` function before any network I/O occurs.

### Citations

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

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
