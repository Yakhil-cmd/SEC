Audit Report

## Title
Unbounded `ingress_expiries` Vec Allocation via Attacker-Controlled `ingress_end` Causes OOM Crash — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`, `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary
Both the ICRC1 and ICP Rosetta `construction_payloads` handlers accept an attacker-supplied `ingress_end` value and enter an unbounded `while` loop that pushes one `u64` per ~2-minute step into a `Vec`. With `ingress_end = u64::MAX`, the loop runs approximately 139 billion iterations, allocating ~1.1 TB before the OS OOM-kills the process. No authentication is required; a single HTTP request is sufficient.

## Finding Description

**ICRC1 path** — `rs/rosetta-api/icrc1/src/construction_api/services.rs`:

`ingress_interval` is computed as `(MAX_INGRESS_TTL − PERMITTED_DRIFT).as_nanos() as u64 = 240_000_000_000 ns`. [1](#0-0) 

The step size inside the loop is `ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64) = 240B − 120B = 120_000_000_000 ns` (2 minutes). [2](#0-1) 

Two guards exist before the loop:
1. `ingress_start >= ingress_end` → error
2. `ingress_end < now + ingress_interval` → error [3](#0-2) 

Guard 2 enforces only a **minimum** for `ingress_end` (~4 minutes in the future). There is **no maximum**. The loop is then entered unconditionally:

```rust
let mut ingress_expiries = vec![];
while ingress_start < ingress_end {
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start +=
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
``` [4](#0-3) 

With `ingress_end = u64::MAX ≈ 18.4×10¹⁸` and `now ≈ 1.75×10¹⁸`, the loop runs `(18.4×10¹⁸ − 1.75×10¹⁸) / 1.2×10¹¹ ≈ 139 billion` iterations, each pushing 8 bytes → **~1.1 TB** of allocation.

**ICP path** — `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`:

The interval is `MAX_INGRESS_TTL − PERMITTED_DRIFT − 120s = 120s`, identical step size. [5](#0-4) 

The loop has **no guards at all** before it — no minimum, no maximum check on `ingress_end`:

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
``` [6](#0-5) 

Constants confirmed: [7](#0-6) 

## Impact Explanation
The Rosetta node process is OOM-killed. All in-flight and queued Rosetta API requests fail immediately. Any exchange, wallet, or tooling relying on this Rosetta endpoint loses service until the process is manually restarted. The attack can be repeated immediately after restart, making recovery impossible without a code fix. This matches the allowed High impact: **"Application/platform-level DoS, crash … or subnet availability impact not based on raw volumetric DDoS"** and **"Significant … Rosetta … infrastructure security impact with concrete user or protocol harm."**

## Likelihood Explanation
The Rosetta HTTP API is publicly accessible by design — it is the standard integration point for exchanges and wallets. No credentials, tokens, or privileged access are required. The malicious payload is a standard JSON body with two integer fields (`ingress_start`, `ingress_end`). The attack is trivially reproducible with a single `curl` command, is non-volumetric (one request suffices), and is immediately repeatable after restart.

## Recommendation
Add an explicit upper-bound check on the ingress window before entering the loop in both files. A reasonable cap is 24 hours:

```rust
const MAX_INGRESS_WINDOW_NS: u64 = 24 * 3600 * 1_000_000_000;

if ingress_end.saturating_sub(ingress_start) > MAX_INGRESS_WINDOW_NS {
    return Err(Error::processing_construction_failed(
        &"Ingress window exceeds maximum allowed duration"
    ));
}
```

Apply the same guard to `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs` before the `while now < ingress_end` loop at line 101.

## Proof of Concept

```bash
curl -X POST http://<rosetta-node>:8080/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"<net_id>"},
    "operations": [<valid_transfer_op>],
    "public_keys": [<valid_pubkey>],
    "metadata": {
      "ingress_start": 1000000000000000000,
      "ingress_end":   18446744073709551615
    }
  }'
```

Unit test confirming the fix:

```rust
#[test]
fn test_ingress_window_too_large_returns_error() {
    let result = construction_payloads(
        valid_ops(),
        Some(ConstructionPayloadsRequestMetadata {
            ingress_start: Some(1_000_000_000_000_000_000),
            ingress_end:   Some(u64::MAX),
            ..Default::default()
        }),
        &ledger_id,
        vec![valid_pubkey()],
        SystemTime::now(),
    );
    assert!(result.is_err(), "must reject oversized ingress window");
}
```

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

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L59-60)
```rust
        let interval =
            ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
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

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```
