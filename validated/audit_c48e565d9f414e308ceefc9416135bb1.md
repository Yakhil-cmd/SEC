Looking at the actual code in `services.rs`, the while loop and its guards are clearly visible. Let me trace the full attack path.

**The while loop** at lines 162–167:

```rust
let mut ingress_expiries = vec![];
while ingress_start < ingress_end {
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start +=
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
```

**The two guards before it** (lines 148–158):

```rust
if ingress_start >= ingress_end {
    return Err(...);  // only rejects if start >= end
}
if ingress_end < now + ingress_interval {
    return Err(...);  // only rejects if end is in the near past
}
```

With `ingress_start=0` and `ingress_end=u64::MAX`:
- Guard 1 passes: `0 < u64::MAX`
- Guard 2 passes: `u64::MAX >= now + ingress_interval`
- `ingress_interval ≈ (MAX_INGRESS_TTL − PERMITTED_DRIFT).as_nanos()` ≈ 240,000,000,000 ns (4 min)
- Loop iterations ≈ `u64::MAX / 240_000_000_000 ≈ 76.8 billion`

Each iteration allocates a `SigningPayload` and an `EnvelopeContent` (containing hex-encoded bytes, canister ID, method args, nonce, sender, ingress_expiry). The process OOMs long before completing.

---

### Title
Unbounded ingress_expiries loop in `construction_payloads` causes OOM crash of Rosetta node — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

### Summary
The `construction_payloads` function in the ICRC-1 Rosetta node builds a `Vec<ingress_expiries>` by iterating a `while ingress_start < ingress_end` loop with no upper-bound guard. An unauthenticated HTTP client can supply `ingress_start=0` and `ingress_end=u64::MAX` in the request metadata, causing the loop to execute ~76 billion iterations and allocate proportional memory, crashing the Rosetta process.

### Finding Description

The `construction_payloads` function in `services.rs` accepts attacker-controlled `ingress_start` and `ingress_end` from `ConstructionPayloadsRequestMetadata` and uses them directly in an unbounded while loop: [1](#0-0) 

The only two guards before the loop are: [2](#0-1) 

Neither guard limits the *size* of the window `ingress_end - ingress_start`. With `ingress_start=0` and `ingress_end=u64::MAX`, both guards pass, and the loop runs approximately `u64::MAX / ingress_interval ≈ 76 billion` iterations. Each iteration pushes one entry into `ingress_expiries`: [1](#0-0) 

That vector is then passed to `handle_construction_payloads`, which allocates a `SigningPayload` and an `EnvelopeContent` per entry: [3](#0-2) 

The `ConstructionPayloadsRequestMetadata` fields `ingress_start` and `ingress_end` are plain `Option<u64>` with no range validation: [4](#0-3) 

### Impact Explanation

A single unauthenticated HTTP POST to `/construction/payloads` with `ingress_start=0, ingress_end=18446744073709551615` causes the Rosetta node process to exhaust all available memory and crash (OOM kill or panic on allocation failure). The Rosetta node is a single-process service; crashing it makes the entire ICRC-1 Rosetta API unavailable until restarted. The IC itself is unaffected.

### Likelihood Explanation

The `/construction/payloads` endpoint is a public, unauthenticated Rosetta API endpoint. No credentials, governance role, or subnet access are required. The attack requires a single HTTP request with two crafted integer fields. It is trivially reproducible.

### Recommendation

Add an explicit cap on the number of ingress expiries before the while loop, for example:

```rust
const MAX_INGRESS_EXPIRIES: usize = 1000; // ~4000 minutes, far beyond any legitimate use

let mut ingress_expiries = vec![];
while ingress_start < ingress_end {
    if ingress_expiries.len() >= MAX_INGRESS_EXPIRIES {
        return Err(Error::processing_construction_failed(
            &"ingress window too large: exceeds maximum allowed expiry count"
        ));
    }
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start += ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
```

Alternatively, validate that `ingress_end - ingress_start` does not exceed a reasonable maximum (e.g., 24 hours in nanoseconds) before entering the loop.

### Proof of Concept

```rust
#[test]
fn test_construction_payloads_unbounded_ingress_window() {
    use std::time::SystemTime;
    // Craft a request with ingress_start=0, ingress_end=u64::MAX
    let metadata = ConstructionPayloadsRequestMetadata {
        ingress_start: Some(0),
        ingress_end: Some(u64::MAX),
        created_at_time: None,
        memo: None,
    };
    // This call should return an error, but instead it will OOM/hang
    let result = construction_payloads(
        vec![/* valid operation */],
        Some(metadata),
        &Principal::anonymous(),
        vec![/* valid public key */],
        SystemTime::UNIX_EPOCH, // now=0 so ingress_end check passes
    );
    // Expected: Err(...) with "ingress window too large"
    // Actual: process runs out of memory
    assert!(result.is_err());
}
```

### Citations

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

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L179-201)
```rust
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
