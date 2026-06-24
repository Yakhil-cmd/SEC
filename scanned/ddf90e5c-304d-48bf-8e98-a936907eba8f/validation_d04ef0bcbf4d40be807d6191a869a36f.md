### Title
Unbounded `ingress_expiries` Vec Allocation via Attacker-Controlled `ingress_end` Causes OOM Crash of Rosetta Node — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`, `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

An unprivileged API client can send a single `POST /construction/payloads` request with `ingress_end = u64::MAX` (or any astronomically large value). The server enters an unbounded `while` loop that pushes one entry per ~2-minute step into a `Vec<u64>`, allocating hundreds of gigabytes of memory before the OS kills the process. No authentication is required. The attack is non-volumetric: one request is sufficient.

---

### Finding Description

**ICRC1 Rosetta path** — `rs/rosetta-api/icrc1/src/construction_api/services.rs`, `construction_payloads()`: [1](#0-0) 

```
ingress_interval = (MAX_INGRESS_TTL − PERMITTED_DRIFT).as_nanos()
                 = (300s − 60s) × 10⁹ = 240_000_000_000 ns
``` [2](#0-1) 

The step size inside the loop is:

```
ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP)
= 240_000_000_000 − 120_000_000_000   // INGRESS_INTERVAL_OVERLAP = 120 s
= 120_000_000_000 ns  (2 minutes)
``` [3](#0-2) 

The only guards before the loop are:

1. `ingress_start >= ingress_end` → error [4](#0-3) 
2. `ingress_end < now + ingress_interval` → error [5](#0-4) 

Guard 2 only enforces a **minimum** for `ingress_end` (at least ~4 minutes in the future). There is **no maximum**. An attacker sets `ingress_end = u64::MAX` and the loop runs:

```
(u64::MAX − now_ns) / 120_000_000_000
≈ (18.4×10¹⁸ − 1.75×10¹⁸) / 1.2×10¹¹
≈ 139 billion iterations
```

Each iteration pushes a `u64` (8 bytes) → **~1.1 TB** of allocation before the process is killed.

**ICP Rosetta path** — `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs` has the identical pattern with the same step size (~2 minutes) and no upper-bound guard: [6](#0-5) [7](#0-6) 

`MAX_INGRESS_TTL = 5 min`, `PERMITTED_DRIFT = 60 s`, confirmed in: [8](#0-7) 

---

### Impact Explanation

The Rosetta node process is killed by OOM. All in-flight and queued Rosetta API requests fail. The IC consensus layer and canisters are unaffected, but any exchange, wallet, or tooling relying on this Rosetta endpoint loses service until the process is restarted. A single request is sufficient; the attack can be repeated immediately after restart.

---

### Likelihood Explanation

The Rosetta HTTP API is publicly accessible by design (it is the integration point for exchanges). No credentials, tokens, or privileged access are required. The malicious payload is a standard JSON body with two integer fields. The attack is trivially reproducible with `curl`.

---

### Recommendation

Add an explicit upper-bound check on the ingress window before entering the loop. A reasonable cap is one or two `MAX_INGRESS_TTL` intervals (e.g., 24 hours = `86_400_000_000_000 ns`):

```rust
const MAX_INGRESS_WINDOW_NS: u64 = 24 * 3600 * 1_000_000_000; // 24 hours

if ingress_end.saturating_sub(ingress_start) > MAX_INGRESS_WINDOW_NS {
    return Err(Error::processing_construction_failed(
        &"Ingress window exceeds maximum allowed duration"
    ));
}
```

Apply the same fix to both `rs/rosetta-api/icrc1/src/construction_api/services.rs` and `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`.

---

### Proof of Concept

```bash
# Single HTTP request — no auth required
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
# Expected: Rosetta process OOM-killed within seconds.
# Invariant violated: payloads.len() grows to ~139 billion instead of being bounded.
```

A unit test confirming the fix:

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

**File:** rs/rosetta-api/icrc1/src/common/constants.rs (L19-19)
```rust
pub const INGRESS_INTERVAL_OVERLAP: Duration = Duration::from_secs(120);
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
