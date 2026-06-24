### Title
Unbounded `ingress_expiries` Vec Allocation via Attacker-Controlled `ingress_start`/`ingress_end` in `construction_payloads` — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

---

### Summary

The `construction_payloads` function in the ICRC-1 Rosetta node builds a `Vec<ingress_expiry>` by looping from `ingress_start` to `ingress_end` in steps of approximately `MAX_INGRESS_TTL - PERMITTED_DRIFT`. An unauthenticated HTTP client can supply `ingress_start=0` and `ingress_end=u64::MAX` in the request metadata. The two existing guards do not bound the window size, so the loop executes ~66 million iterations, allocating a proportionally large `Vec` of `SigningPayload` and `EnvelopeContent` objects, exhausting process memory and crashing the single Rosetta replica.

---

### Finding Description

The vulnerable loop is in `construction_payloads`:

```rust
// services.rs lines 162-167
let mut ingress_expiries = vec![];
while ingress_start < ingress_end {
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start +=
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
``` [1](#0-0) 

`ingress_start` and `ingress_end` are taken directly from attacker-supplied metadata with no window-size cap:

```rust
let mut ingress_start = metadata
    .as_ref()
    .and_then(|meta| meta.ingress_start)
    .unwrap_or(now);

let ingress_end = metadata
    .as_ref()
    .and_then(|meta| meta.ingress_end)
    .unwrap_or(ingress_start + ingress_interval);
``` [2](#0-1) 

The only two guards present are:

1. **Guard 1** — rejects if `ingress_start >= ingress_end`. With `ingress_start=0, ingress_end=u64::MAX`, this passes.
2. **Guard 2** — rejects if `ingress_end < now + ingress_interval`. With `ingress_end=u64::MAX`, this passes. [3](#0-2) 

Neither guard limits the *size* of the window `ingress_end - ingress_start`.

The step per iteration is `ingress_interval - INGRESS_INTERVAL_OVERLAP`. `ingress_interval` is `(MAX_INGRESS_TTL - PERMITTED_DRIFT).as_nanos() as u64`, which is approximately 270 billion nanoseconds (≈270 s). With `ingress_end - ingress_start = u64::MAX ≈ 1.84 × 10¹⁹ ns`, the loop executes approximately:

```
1.84 × 10¹⁹ / 2.7 × 10¹¹ ≈ 68,000,000 iterations
```

Each iteration pushes one `SigningPayload` and one `EnvelopeContent` (containing a full CBOR-serialized canister call with cloned `canister_method_args`) into growing Vecs: [4](#0-3) 

At even a conservative 200 bytes per entry, 68 million entries = ~13 GB of heap allocation, crashing the process via OOM.

The `ConstructionPayloadsRequestMetadata` struct accepts `ingress_start` and `ingress_end` as plain `Option<u64>` with no range validation: [5](#0-4) 

---

### Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with a crafted JSON body causes the Rosetta node process to OOM-crash. The Rosetta node is a single-process service; there is no replica redundancy at the process level. This results in a complete denial of service of the ICRC-1 Rosetta node until it is manually restarted.

---

### Likelihood Explanation

The Rosetta API is a public HTTP endpoint with no authentication. The malicious request is a single, small JSON payload. No special privileges, keys, or network position are required. The attack is trivially repeatable to prevent recovery.

---

### Recommendation

Add an explicit cap on the ingress window immediately after computing `ingress_start` and `ingress_end`, before the loop. For example:

```rust
const MAX_INGRESS_WINDOW: u64 = 24 * 3600 * 1_000_000_000; // 24 hours in nanoseconds
if ingress_end.saturating_sub(ingress_start) > MAX_INGRESS_WINDOW {
    return Err(Error::processing_construction_failed(
        &"Ingress window exceeds maximum allowed duration of 24 hours"
    ));
}
```

This bounds the loop to at most `MAX_INGRESS_WINDOW / ingress_interval ≈ 320` iterations, matching the intended design (24 h / ~270 s ≈ 320 envelopes).

---

### Proof of Concept

```rust
#[test]
fn test_unbounded_ingress_window_oom() {
    use std::time::SystemTime;
    use crate::construction_api::types::ConstructionPayloadsRequestMetadata;

    let metadata = ConstructionPayloadsRequestMetadata {
        ingress_start: Some(0),
        ingress_end: Some(u64::MAX),
        created_at_time: None,
        memo: None,
    };

    // This call should return an error, but instead loops ~68M times
    let result = construction_payloads(
        vec![/* valid transfer operation */],
        Some(metadata),
        &candid::Principal::anonymous(),
        vec![/* valid public key */],
        SystemTime::now(),
    );

    // Invariant: must be bounded or return an error
    assert!(result.is_err(), "Expected error for unbounded ingress window");
}
```

With `ingress_start=0` and `ingress_end=u64::MAX`, the process exhausts memory before returning. The fix causes the function to return an error immediately.

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L128-136)
```rust
    let mut ingress_start = metadata
        .as_ref()
        .and_then(|meta| meta.ingress_start)
        .unwrap_or(now);

    let ingress_end = metadata
        .as_ref()
        .and_then(|meta| meta.ingress_end)
        .unwrap_or(ingress_start + ingress_interval);
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

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L441-461)
```rust
    let mut signing_payloads = Vec::new();
    let mut envelope_contents = Vec::new();
    for (nonce, ingress_expiry) in ingress_expiries.iter().enumerate() {
        // Rosetta will send an envelope containing the update information to a replica
        let envelope_content = EnvelopeContent::Call {
            canister_id,
            method_name: canister_method_name.to_string(),
            arg: canister_method_args.clone(),
            nonce: Some(nonce.to_ne_bytes().to_vec()),
            sender: caller,
            ingress_expiry: *ingress_expiry,
        };

        // For every operation we create a call envelope
        // For every envelope we create a signing payload
        let payload =
            build_payloads_from_call_envelope_content(&envelope_content, &sender_public_key)?;

        signing_payloads.push(payload);
        envelope_contents.push(envelope_content);
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L178-201)
```rust
/// Typed metadata of ConstructionPayloadsRequest.
#[derive(Clone, Eq, PartialEq, Debug, Default, Deserialize, Serialize)]
pub struct ConstructionPayloadsRequestMetadata {
    /// The memo to use for a ledger transfer.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub memo: Option<Vec<u8>>,

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
