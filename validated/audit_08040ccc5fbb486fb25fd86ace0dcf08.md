Audit Report

## Title
Unbounded While-Loop via Attacker-Controlled `ingress_start` Causes Memory Exhaustion in ICRC1 Rosetta `construction_payloads` — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

## Summary
The public `POST /construction/payloads` endpoint in the ICRC1 Rosetta server accepts attacker-controlled `ingress_start` and `ingress_end` values with no lower-bound validation on `ingress_start`. Setting `ingress_start = 1` and `ingress_end = now + ingress_interval` passes both existing guards and causes the while-loop in `construction_payloads` to iterate approximately 14.6 million times, allocating a full `EnvelopeContent::Call` struct with a cloned `canister_method_args` blob per iteration, exhausting process memory and crashing the server.

## Finding Description
In `rs/rosetta-api/icrc1/src/construction_api/services.rs`, `ingress_start` is taken verbatim from the request metadata with no lower-bound check: [1](#0-0) 

The two guards only reject `ingress_start >= ingress_end` and `ingress_end < now + ingress_interval`: [2](#0-1) 

Neither guard prevents `ingress_start` from being set to `1`. The while-loop then iterates from `ingress_start` to `ingress_end` in steps of `ingress_interval - INGRESS_INTERVAL_OVERLAP`: [3](#0-2) 

`INGRESS_INTERVAL_OVERLAP` is 120 seconds: [4](#0-3) 

`ingress_interval = MAX_INGRESS_TTL - PERMITTED_DRIFT = 300s - 60s = 240s`. Step size = `240s - 120s = 120s = 1.2 × 10¹¹ ns`. With `ingress_start = 1` and `ingress_end ≈ 1.75 × 10¹⁸ ns` (current epoch time in 2026), the loop runs ≈ **14.6 million iterations**. Each iteration in `handle_construction_payloads` allocates an `EnvelopeContent::Call` with a cloned `canister_method_args`: [5](#0-4) 

The endpoint handler passes `SystemTime::now()` and the raw metadata directly to `services::construction_payloads` with no pre-validation: [6](#0-5) 

No rate limiting, request body size cap, or iteration cap exists anywhere in the ICRC1 Rosetta server path.

## Impact Explanation
A single unauthenticated HTTP POST causes the ICRC1 Rosetta server process to allocate gigabytes of memory (millions of `EnvelopeContent::Call` structs, each containing a cloned `Vec<u8>`), leading to OOM crash or severe memory pressure. This constitutes an **application/platform-level DoS** against the ICRC1 Rosetta API, which is explicitly in-scope as a financial integration component. This maps to **High ($2,000–$10,000)**: "Application/platform-level DoS, crash... or subnet availability impact not based on raw volumetric DDoS" and "Significant... Rosetta... security impact with concrete user or protocol harm."

## Likelihood Explanation
The attack requires only a single unauthenticated HTTP POST to `/construction/payloads` with two crafted JSON fields (`ingress_start: 1`, `ingress_end: <now_ns + 240_000_000_000>`). No credentials, keys, or special access are needed. The endpoint is publicly reachable. The attack is trivially reproducible and repeatable — the server can be crashed repeatedly after each restart.

## Recommendation
Add a lower-bound guard on `ingress_start` before the while-loop, rejecting requests where `ingress_start` is more than one interval in the past:

```rust
if ingress_start + ingress_interval < now {
    return Err(Error::processing_construction_failed(&format!(
        "ingress_start is too far in the past: {ingress_start}"
    )));
}
```

Additionally, cap the maximum number of loop iterations (e.g., `MAX_INGRESS_EXPIRIES = 720` for a 24-hour window at 120s steps) and return an error if the computed count would exceed it.

## Proof of Concept
Send the following HTTP request to a running ICRC1 Rosetta server (replace `<now_ns>` with current Unix time in nanoseconds, e.g., `1_750_000_000_000_000_000`):

```
POST /construction/payloads HTTP/1.1
Content-Type: application/json

{
  "network_identifier": { "blockchain": "Internet Computer", "network": "<ledger_canister_id>" },
  "operations": [ <valid ICRC1 transfer operation> ],
  "public_keys": [ <valid Ed25519 public key> ],
  "metadata": {
    "ingress_start": 1,
    "ingress_end": <now_ns + 240000000000>
  }
}
```

The while-loop at lines 163–167 of `services.rs` runs ≈14.6 million iterations. Each iteration allocates an `EnvelopeContent::Call` with a cloned `canister_method_args` blob. The process OOMs before returning a response. This can be verified locally with a unit test by calling `construction_payloads(ops, Some(metadata_with_ingress_start_1), ledger_id, keys, SystemTime::now())` and observing that `ingress_expiries.len()` reaches ~14.6 million before the function returns.

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L128-131)
```rust
    let mut ingress_start = metadata
        .as_ref()
        .and_then(|meta| meta.ingress_start)
        .unwrap_or(now);
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

**File:** rs/rosetta-api/icrc1/src/common/constants.rs (L19-19)
```rust
pub const INGRESS_INTERVAL_OVERLAP: Duration = Duration::from_secs(120);
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
