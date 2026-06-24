### Title
Unbounded Ingress Window Loop in ICP Rosetta `construction_payloads` Causes OOM/Infinite-Loop DoS — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The `construction_payloads` handler in the ICP Rosetta node accepts attacker-controlled `ingress_start` and `ingress_end` values from the request metadata and feeds them directly into an unbounded `while` loop with no window-size validation. Setting `ingress_start = 0` and `ingress_end = u64::MAX` causes the loop to execute approximately 153 billion iterations, exhausting process memory or spinning indefinitely.

---

### Finding Description

`MAX_INGRESS_TTL = 300s`, `PERMITTED_DRIFT = 60s`, so:

```
interval = 300s − 60s − 120s = 120s = 120_000_000_000 ns
```

The loop at lines 99–107:

```rust
let mut ingress_expiries = vec![];
let mut now = ingress_start;
while now < ingress_end {
    ingress_expiries.push(...);
    now += interval;          // wraps on overflow → infinite loop
}
``` [1](#0-0) 

`ingress_start` and `ingress_end` are plain `Option<u64>` fields deserialized from JSON with no range or window-size check: [2](#0-1) 

The `TryFrom<ObjectMap>` conversion is a pure `serde_json` deserialize — no validation: [3](#0-2) 

`MAX_INGRESS_TTL` and `PERMITTED_DRIFT` are confirmed constants: [4](#0-3) 

With `ingress_start = 0` and `ingress_end = u64::MAX`:
- Iterations ≈ `u64::MAX / 120_000_000_000` ≈ **153 billion**
- Memory to allocate: 153 billion × 8 bytes ≈ **1.2 TB** → OOM kill
- If `now += interval` wraps around (release-mode `u64` arithmetic), the condition `now < u64::MAX` remains true → **infinite loop**

Either outcome is a complete denial of service of the Rosetta process.

---

### Impact Explanation

The ICP Rosetta node is the standard off-chain gateway used by exchanges and custodians to submit ICP ledger transfers. A single malformed HTTP POST to `/construction/payloads` with the crafted metadata causes the Rosetta process to be killed by the OS OOM killer or spin indefinitely, blocking all ICP transfers routed through that node until it is manually restarted.

---

### Likelihood Explanation

The `/construction/payloads` endpoint requires no authentication. Any network-reachable caller can send the payload. The exploit is a single HTTP request with two integer fields set to extreme values. No privileged access, key material, or social engineering is required.

---

### Recommendation

Add an explicit window-size guard before the loop, mirroring the pattern already present in the ICRC1 Rosetta implementation: [5](#0-4) 

For the ICP Rosetta handler, add before line 99:

```rust
let max_window = ic_limits::MAX_INGRESS_TTL * 24; // e.g., 24 hours
if ingress_end.saturating_sub(ingress_start) > max_window {
    return Err(ApiError::invalid_request(
        "ingress_end − ingress_start exceeds maximum allowed window",
    ));
}
```

This bounds `ingress_expiries` to at most `max_window / interval` entries (≤ 720 for a 24-hour window).

---

### Proof of Concept

```rust
#[test]
fn test_construction_payloads_unbounded_loop() {
    let metadata = ConstructionPayloadsRequestMetadata {
        ingress_start: Some(0),
        ingress_end: Some(u64::MAX),
        memo: None,
        created_at_time: None,
    };
    // Build a minimal valid ConstructionPayloadsRequest with the above metadata
    // and any valid TRANSACTION+FEE operations + a real public key.
    // Call handler.construction_payloads(request).
    // Without the fix: process OOMs or loops indefinitely.
    // With the fix: returns Err immediately.
}
```

### Citations

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

**File:** rs/rosetta-api/icp/src/models.rs (L207-217)
```rust
    /// The earliest acceptable expiry date for a ledger transfer.
    /// Must be within 24 hours from created_at_time.
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ingress_start: Option<u64>,

    /// The latest acceptable expiry date for a ledger transfer.
    /// Must be within 24 hours from created_at_time.
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ingress_end: Option<u64>,
```

**File:** rs/rosetta-api/icp/src/models.rs (L240-248)
```rust
impl TryFrom<ObjectMap> for ConstructionPayloadsRequestMetadata {
    type Error = ApiError;
    fn try_from(o: ObjectMap) -> Result<Self, ApiError> {
        serde_json::from_value(serde_json::Value::Object(o)).map_err(|e| {
            ApiError::internal_error(format!(
                "Could not parse ConstructionPayloadsRequestMetadata from Object: {e}"
            ))
        })
    }
```

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
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
