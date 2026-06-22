### Title
Unbounded Ingress Window Causes Uncapped Memory Allocation in `/construction/payloads` - (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

### Summary

The `construction_payloads` function accepts attacker-controlled `ingress_start` and `ingress_end` values with no upper bound on the window size. The while loop that populates `ingress_expiries` runs one iteration per `~150-second` step across the entire window, and `handle_construction_payloads` allocates one `EnvelopeContent::Call` (with a full clone of `canister_method_args`) per entry. There is no cap on how large the window can be, so an unprivileged HTTP client can trigger unbounded heap allocation and produce an arbitrarily large CBOR+hex response.

### Finding Description

In `construction_payloads` (services.rs), the only validations on the ingress window are:

- `ingress_start >= ingress_end` → reject [1](#0-0) 
- `ingress_end < now + ingress_interval` → reject [2](#0-1) 

There is **no maximum window size check**. The while loop then runs unboundedly:

```rust
while ingress_start < ingress_end {
    ingress_expiries.push(ingress_start + ingress_interval);
    ingress_start +=
        ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
}
``` [3](#0-2) 

The step size is `ingress_interval − INGRESS_INTERVAL_OVERLAP`. With `MAX_INGRESS_TTL = 5 min`, `PERMITTED_DRIFT = 30 s`, and `INGRESS_INTERVAL_OVERLAP = 120 s`:

- `ingress_interval` = 270 s → step = **150 seconds**
- 24-hour window → **576 entries** (intended design, documented in types.rs line 44)
- 1-year window → **~210,240 entries**
- 10-year window → **~2,102,400 entries**

`handle_construction_payloads` then allocates one `EnvelopeContent::Call` per entry, **cloning `canister_method_args` each time**: [4](#0-3) 

The `UnsignedTransaction` is then CBOR-serialized and hex-encoded (doubling the byte size) before being returned in the HTTP response: [5](#0-4) 

The endpoint handler passes `ingress_start`/`ingress_end` directly from the JSON request body with no additional guards: [6](#0-5) 

### Impact Explanation

An unprivileged HTTP client with no credentials can POST a single small JSON request to `/construction/payloads` with `ingress_end = now + N years`. The server will:
1. Allocate O(N × 365 × 86400 / 150) `EnvelopeContent` structs on the heap, each containing a full clone of the ICRC1 transfer args.
2. CBOR-serialize and hex-encode the entire vector into a single response string.
3. Exhaust available memory, causing OOM and crashing the Rosetta sidecar process.

### Likelihood Explanation

The endpoint requires no authentication. The request payload is trivially small. The `ConstructionPayloadsRequestMetadata` struct accepts `ingress_start` and `ingress_end` as plain `Option<u64>` nanosecond timestamps with no domain-level constraints enforced at deserialization time. [7](#0-6) 

### Recommendation

Add an explicit upper bound on the ingress window before the while loop, e.g.:

```rust
const MAX_INGRESS_WINDOW: u64 = 24 * 3600 * 1_000_000_000; // 24 hours in nanoseconds
if ingress_end - ingress_start > MAX_INGRESS_WINDOW {
    return Err(Error::processing_construction_failed(
        "Ingress window exceeds maximum allowed 24-hour range"
    ));
}
```

This matches the documented design intent stated in `types.rs` line 44 and caps the vector at ~576 entries.

### Proof of Concept

```bash
curl -X POST http://<rosetta-host>:8081/construction/payloads \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"<ledger_id>"},
    "operations": [/* valid ICRC1 transfer ops */],
    "public_keys": [{"hex_bytes":"<pubkey>","curve_type":"edwards25519"}],
    "metadata": {
      "ingress_start": <now_ns>,
      "ingress_end":   <now_ns + 10_years_in_ns>
    }
  }'
```

With `ingress_end − ingress_start = 10 years ≈ 3.15 × 10¹⁷ ns`, the loop runs ~2,102,400 iterations, each cloning the transfer args and building an `EnvelopeContent`. The hex-encoded CBOR response will be several gigabytes, exhausting server memory before the response is even written.

### Citations

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

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L163-167)
```rust
    while ingress_start < ingress_end {
        ingress_expiries.push(ingress_start + ingress_interval);
        ingress_start +=
            ingress_interval.saturating_sub(INGRESS_INTERVAL_OVERLAP.as_nanos() as u64);
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L443-461)
```rust
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

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L149-153)
```rust
impl std::fmt::Display for UnsignedTransaction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", hex::encode(serde_cbor::ser::to_vec(self).unwrap()))
    }
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

**File:** rs/rosetta-api/icrc1/src/construction_api/endpoints.rs (L90-107)
```rust
pub async fn construction_payloads(
    State(state): State<Arc<MultiTokenAppState>>,
    Json(request): Json<ConstructionPayloadsRequest>,
) -> Result<Json<ConstructionPayloadsResponse>> {
    let state = get_state_from_network_id(&request.network_identifier, &state)
        .map_err(|err| Error::invalid_network_id(&err))?;
    Ok(Json(services::construction_payloads(
        request.operations,
        request
            .metadata
            .as_ref()
            .map(|m| ConstructionPayloadsRequestMetadata::try_from(m.clone()))
            .transpose()?,
        &state.icrc1_agent.ledger_canister_id,
        request.public_keys.unwrap_or_else(Vec::new),
        SystemTime::now(),
    )?))
}
```
