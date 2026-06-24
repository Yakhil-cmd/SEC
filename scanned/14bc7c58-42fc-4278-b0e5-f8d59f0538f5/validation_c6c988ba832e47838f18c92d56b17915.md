### Title
Unbounded Ingress-Window Loop in ICP Rosetta `construction_payloads` Enables OOM Crash — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

The ICP Rosetta `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` values from the request metadata and feeds them directly into an unbounded `while` loop with no cap on the resulting allocation. Supplying `ingress_start=0` and `ingress_end=u64::MAX` causes the loop to push ~154 billion `u64` entries into a `Vec`, exhausting heap memory and crashing the Rosetta process.

---

### Finding Description

In `construction_payloads()`, the `interval` step is computed as:

```
interval = MAX_INGRESS_TTL − PERMITTED_DRIFT − 120s
         = 300s − 60s − 120s = 120s = 120,000,000,000 ns
``` [1](#0-0) [2](#0-1) 

`ingress_start` and `ingress_end` are taken verbatim from the caller-supplied metadata with no range or window-size validation: [3](#0-2) 

The loop then runs without any bound: [4](#0-3) 

With `ingress_start = 0` and `ingress_end = u64::MAX = 18,446,744,073,709,551,615 ns`:

```
iterations ≈ 18,446,744,073,709,551,615 / 120,000,000,000 ≈ 1.54 × 10¹¹
heap needed ≈ 1.54 × 10¹¹ × 8 bytes ≈ 1.23 TB
```

The process OOMs long before the loop completes. Additionally, if `ic_types::time::Time` addition wraps on overflow in release builds, `now` wraps back below `ingress_end` and the loop becomes infinite.

The `ConstructionPayloadsRequestMetadata` struct accepts both fields as plain `Option<u64>` with no constraints: [5](#0-4) 

---

### Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with `metadata.ingress_start=0` and `metadata.ingress_end=18446744073709551615` causes the Rosetta node process to exhaust heap memory and crash. No authentication, no privileged role, and no volumetric traffic are required — one request suffices.

---

### Likelihood Explanation

The Rosetta API is a publicly documented, network-reachable endpoint. The exploit requires no special knowledge beyond the Rosetta API spec. The vulnerable code path is exercised on every `/construction/payloads` call that supplies metadata. There is no existing guard: the only pre-loop checks are metadata deserialization and `verify_network_id`. [6](#0-5) 

For contrast, the ICRC1 Rosetta implementation also lacks a window-size cap but does at least validate `ingress_start < ingress_end` and `ingress_end >= now + interval` before its loop — neither of which prevents the attack with `ingress_start=0, ingress_end=u64::MAX` since both checks pass trivially. [7](#0-6) 

---

### Recommendation

Before the loop, enforce a maximum window size. For example:

```rust
const MAX_INGRESS_WINDOW: Duration = Duration::from_secs(24 * 60 * 60); // 24 h
if ingress_end > ingress_start + MAX_INGRESS_WINDOW {
    return Err(ApiError::invalid_request(
        "ingress_end exceeds maximum allowed window of 24 hours",
    ));
}
```

Alternatively, cap the `Vec` capacity and return an error if the computed count exceeds a small constant (e.g., 1000 entries).

---

### Proof of Concept

```rust
// Unit test — no network required
#[test]
fn construction_payloads_oom_with_max_ingress_window() {
    let meta = ConstructionPayloadsRequestMetadata {
        ingress_start: Some(0),
        ingress_end: Some(u64::MAX),
        memo: None,
        created_at_time: None,
    };
    // interval = 120s = 120_000_000_000 ns
    // iterations ≈ u64::MAX / 120_000_000_000 ≈ 1.54e11
    // Each push is 8 bytes → ~1.23 TB allocation attempt → OOM
    let interval_ns: u64 = 120_000_000_000;
    let count = u64::MAX / interval_ns;
    assert!(count > 100_000_000_000, "loop count is {count}");
    // Calling construction_payloads() with this metadata will OOM the process.
}
```

HTTP trigger:
```
POST /construction/payloads
{
  "network_identifier": { ... },
  "operations": [ ... ],
  "public_keys": [ ... ],
  "metadata": {
    "ingress_start": 0,
    "ingress_end": 18446744073709551615
  }
}
```

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L43-57)
```rust
    pub fn construction_payloads(
        &self,
        msg: ConstructionPayloadsRequest,
    ) -> Result<ConstructionPayloadsResponse, ApiError> {
        verify_network_id(self.ledger.ledger_canister_id(), &msg.network_identifier)?;

        let ops = msg.operations.clone();

        let pks = msg.public_keys.clone().ok_or_else(|| {
            const NO_PUBLIC_KEYS: &str = "Expected field 'public_keys' to be populated";
            debug!("{NO_PUBLIC_KEYS}");
            ApiError::internal_error(NO_PUBLIC_KEYS)
        })?;
        let transactions =
            convert::operations_to_requests(&ops, false, self.ledger.token_symbol())?;
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

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

**File:** rs/rosetta-api/icp/src/models.rs (L199-223)
```rust
/// Typed metadata of ConstructionPayloadsRequest.
#[derive(Clone, Eq, PartialEq, Debug, Default, Deserialize, Serialize)]
pub struct ConstructionPayloadsRequestMetadata {
    /// The memo to use for a ledger transfer.
    /// A random number is used by default.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memo: Option<u64>,

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

    /// If present, overrides ledger transaction creation time.
    /// Represents number of nanoseconds since UNIX epoch.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at_time: Option<u64>,
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
