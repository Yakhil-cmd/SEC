### Title
Unbounded `ingress_expiries` Loop via Attacker-Controlled `ingress_end=u64::MAX` Causes OOM Crash — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

---

### Summary

The `construction_payloads` function in the ICRC1 Rosetta service accepts attacker-controlled `ingress_start` and `ingress_end` values from the JSON body of `POST /construction/payloads`. Two guards exist before the loop, but neither caps an arbitrarily large window. Supplying `ingress_end = u64::MAX` bypasses both guards and causes the while-loop at lines 163–167 to execute ~139 million iterations before `ingress_start` overflows u64 and wraps to zero, after which the loop becomes infinite. The Rosetta process exhausts heap memory and crashes.

---

### Finding Description

**Relevant constants** (from `rs/limits/src/lib.rs` and `rs/rosetta-api/icrc1/src/common/constants.rs`):

| Constant | Value |
|---|---|
| `MAX_INGRESS_TTL` | 300 s = 3×10¹¹ ns |
| `PERMITTED_DRIFT` | 60 s = 6×10¹⁰ ns |
| `ingress_interval` | 240 s = 2.4×10¹¹ ns |
| `INGRESS_INTERVAL_OVERLAP` | 120 s = 1.2×10¹¹ ns |
| **Loop step** | 240 s − 120 s = **120 s = 1.2×10¹¹ ns** | [1](#0-0) [2](#0-1) 

**Guard 1** (line 148): rejects if `ingress_start >= ingress_end`. With `ingress_start = now ≈ 1.75×10¹⁸` and `ingress_end = u64::MAX ≈ 1.844×10¹⁹`, this is `FALSE` → passes. [3](#0-2) 

**Guard 2** (line 154): rejects if `ingress_end < now + ingress_interval`. With `ingress_end = u64::MAX`, this evaluates to `1.844×10¹⁹ < 1.75×10¹⁸ + 2.4×10¹¹` → `FALSE` → passes. [4](#0-3) 

**The loop** (lines 163–167) then runs with no upper-bound check:

```rust
while ingress_start < ingress_end {
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start +=
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
``` [5](#0-4) 

- **Phase 1 — OOM:** `(u64::MAX − now) / step ≈ 1.669×10¹⁹ / 1.2×10¹¹ ≈ 139,000,000` iterations, each pushing a `u64` (8 bytes) → **~1.1 GB** of heap allocation before overflow.
- **Phase 2 — Infinite loop:** In Rust release builds, `u64` addition wraps on overflow. Once `ingress_start` wraps to a small value, `ingress_start < u64::MAX` is permanently `true`, making the loop infinite. The process either OOMs first or spins forever.

---

### Impact Explanation

The Rosetta process crashes (OOM or infinite loop consuming all memory). Any user of that ICRC1 Rosetta node loses access to the Construction API. A single unauthenticated HTTP request is sufficient to trigger this.

---

### Likelihood Explanation

The ICRC1 Rosetta HTTP port is publicly reachable by design. No authentication, no rate-limiting on this endpoint, and no privileged role is required. The attacker only needs to craft a valid JSON body with `ingress_end` set to a large integer. The exploit is deterministic and reproducible locally.

---

### Recommendation

Add an explicit cap on the number of ingress expiries immediately before the loop:

```rust
const MAX_INGRESS_EXPIRIES: usize = 1000; // ~33 hours at 120s step

let window = ingress_end.saturating_sub(ingress_start);
let step = ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
let estimated_count = (window / step).saturating_add(1) as usize;
if estimated_count > MAX_INGRESS_EXPIRIES {
    return Err(Error::processing_construction_failed(
        &format!("Ingress window too large: would produce {estimated_count} envelopes (max {MAX_INGRESS_EXPIRIES})")
    ));
}
```

This must be checked **before** the loop, not inside it.

---

### Proof of Concept

```rust
#[test]
fn test_construction_payloads_oom_via_max_ingress_end() {
    use std::time::{Duration, SystemTime, UNIX_EPOCH};
    let now = SystemTime::now();
    let now_ns = now.duration_since(UNIX_EPOCH).unwrap().as_nanos() as u64;

    let meta = ConstructionPayloadsRequestMetadata {
        ingress_start: Some(now_ns),
        ingress_end: Some(u64::MAX),  // attacker-controlled
        ..Default::default()
    };

    // This call must return Err before allocating unbounded memory.
    let result = construction_payloads(
        valid_operations(),
        Some(meta),
        &Principal::anonymous(),
        vec![test_public_key()],
        now,
    );
    assert!(result.is_err(), "Must reject oversized ingress window");
}
``` [6](#0-5)

### Citations

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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L111-167)
```rust
pub fn construction_payloads(
    operations: Vec<Operation>,
    metadata: Option<ConstructionPayloadsRequestMetadata>,
    ledger_id: &Principal,
    public_keys: Vec<PublicKey>,
    now: SystemTime,
) -> Result<ConstructionPayloadsResponse, Error> {
    // The interval between each ingress message
    // The permitted drift makes sure that intervals are overlapping and there are no edge cases when trying to submit to the IC
    let ingress_interval: u64 =
        (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos() as u64;

    let now = now
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .as_nanos() as u64;

    let mut ingress_start = metadata
        .as_ref()
        .and_then(|meta| meta.ingress_start)
        .unwrap_or(now);

    let ingress_end = metadata
        .as_ref()
        .and_then(|meta| meta.ingress_end)
        .unwrap_or(ingress_start + ingress_interval);

    let created_at_time = metadata
        .as_ref()
        .and_then(|meta| meta.created_at_time)
        .unwrap_or(now);

    let memo = metadata
        .as_ref()
        .and_then(|meta| meta.memo.clone())
        .map(|memo| memo.into());

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

    // Every ingress message sent to the IC has an expiry timestamp until which the signature associated with that message is valid
    // To support a longer overall timeframe than one interval, we can send multiple ingress messages with two signable contents each
    let mut ingress_expiries = vec![];
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
```
