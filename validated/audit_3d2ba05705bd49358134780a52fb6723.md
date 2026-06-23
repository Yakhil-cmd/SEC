The vulnerability is real. Let me confirm the exact overflow/wrap behavior of `Time::add_assign` before writing the final assessment.

The `AddAssign<Duration>` for `Time` is:

### Title
Unbounded Ingress Window Causes OOM / Infinite Loop in ICP Rosetta `construction_payloads` — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` values from the HTTP request metadata with no bounds check on the window size. Setting `ingress_start=0` and `ingress_end=u64::MAX` causes the internal while loop to run for ~153 billion iterations before OOM, or to loop infinitely due to a truncating `as u64` cast in `Time::from_duration`.

---

### Finding Description

`construction_payloads` computes `interval` as:

```
MAX_INGRESS_TTL(300s) - PERMITTED_DRIFT(60s) - 120s = 120s = 120_000_000_000 ns
``` [1](#0-0) 

It then directly uses the caller-supplied `ingress_start` and `ingress_end` without any window-size validation: [2](#0-1) 

The loop that follows has no guard: [3](#0-2) 

`now += interval` calls `Time::AddAssign<Duration>`: [4](#0-3) 

which calls `Time::from_duration`: [5](#0-4) 

The `t.as_nanos()` returns `u128`; the `as u64` cast **silently truncates**. `Duration` itself does not overflow (it stores seconds as `u64`, far exceeding `u64::MAX` nanoseconds), so when `now.0` is near `u64::MAX`, the Duration addition succeeds but the truncating cast wraps `now` back to a small value — making the loop **infinite**.

Before the wrap-around is reached, the loop pushes one `u64` per iteration into `ingress_expiries`:

```
u64::MAX / 120_000_000_000 ≈ 153 billion iterations × 8 bytes ≈ 1.2 TB
```

The process OOMs and crashes well before the wrap point.

The ICRC1 Rosetta variant (`rs/rosetta-api/icrc1/src/construction_api/services.rs`) has partial guards (rejects `ingress_start >= ingress_end` and stale `ingress_end`) but also has no upper bound on the window size, making it independently vulnerable to the same class of attack. [6](#0-5) 

The `MAX_INGRESS_TTL` and `PERMITTED_DRIFT` constants are confirmed: [7](#0-6) 

---

### Impact Explanation

An attacker who can reach the Rosetta HTTP endpoint sends a single POST to `/construction/payloads` with `metadata.ingress_start=0` and `metadata.ingress_end=18446744073709551615` plus any syntactically valid TRANSACTION+FEE operations and a valid public key. The Rosetta node process exhausts all available memory and crashes (or hangs in an infinite loop), making the node unavailable. All ICP transfers routed through that node are blocked for the duration of the outage. If the node is shared infrastructure (e.g., an exchange's Rosetta instance), all users of that instance are affected.

---

### Likelihood Explanation

The Rosetta HTTP API is intentionally exposed to clients. No authentication is required to call `/construction/payloads`. The exploit requires a single HTTP request with a crafted JSON body — no privileged access, no key material, no network-level attack. The only precondition is network reachability to the Rosetta port.

---

### Recommendation

Before entering the while loop, validate that the ingress window does not exceed a reasonable maximum (e.g., `MAX_INGRESS_TTL` itself, or some small multiple of it):

```rust
let max_window = ic_limits::MAX_INGRESS_TTL * 2; // or any reasonable bound
if ingress_end > ingress_start + max_window {
    return Err(ApiError::invalid_request(
        "ingress_end - ingress_start exceeds maximum allowed window"
    ));
}
```

Additionally, replace the truncating `as u64` cast in `Time::from_duration` with a checked or saturating conversion to prevent silent wrap-around: [5](#0-4) 

---

### Proof of Concept

```rust
#[test]
fn test_construction_payloads_oom_dos() {
    use std::time::Duration;
    let handler = /* build RosettaRequestHandler */;
    let req = ConstructionPayloadsRequest {
        network_identifier: /* valid network */,
        operations: vec![/* valid TRANSACTION + FEE ops */],
        public_keys: Some(vec![/* valid public key */]),
        metadata: Some(serde_json::json!({
            "ingress_start": 0u64,
            "ingress_end": u64::MAX,
        }).as_object().cloned()),
        ..Default::default()
    };
    // This call should return Err(...) within bounded time.
    // Without the fix it OOMs or loops forever.
    let result = handler.construction_payloads(req);
    assert!(result.is_err(), "expected rejection of unbounded ingress window");
}
```

### Citations

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

**File:** rs/types/types/src/time.rs (L55-59)
```rust
impl std::ops::AddAssign<Duration> for Time {
    fn add_assign(&mut self, other: Duration) {
        *self = Time::from_duration(Duration::from_nanos(self.0) + other)
    }
}
```

**File:** rs/types/types/src/time.rs (L103-105)
```rust
    fn from_duration(t: Duration) -> Self {
        Time(t.as_nanos() as u64)
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L148-167)
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

    // Every ingress message sent to the IC has an expiry timestamp until which the signature associated with that message is valid
    // To support a longer overall timeframe than one interval, we can send multiple ingress messages with two signable contents each
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
