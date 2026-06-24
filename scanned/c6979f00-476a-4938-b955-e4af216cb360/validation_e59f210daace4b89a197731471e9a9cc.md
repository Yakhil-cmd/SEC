### Title
Unbounded While-Loop via Attacker-Controlled `ingress_start=0` Causes Memory Exhaustion in ICRC1 Rosetta `construction_payloads` — (`rs/rosetta-api/icrc1/src/construction_api/endpoints.rs`)

---

### Summary

The public HTTP endpoint `POST /construction/payloads` in the ICRC1 Rosetta server accepts attacker-controlled `ingress_start` and `ingress_end` values. The two guards only reject requests where `ingress_start >= ingress_end` or `ingress_end < now + ingress_interval`. There is **no lower bound check on `ingress_start`**. Setting `ingress_start = 1` and `ingress_end = now + ingress_interval` passes both guards and causes the while-loop to iterate approximately **14–15 million times**, allocating a full `EnvelopeContent::Call` struct (with cloned `canister_method_args`) per iteration, exhausting process memory.

---

### Finding Description

The endpoint handler in `endpoints.rs` passes metadata directly to `services::construction_payloads`: [1](#0-0) 

Inside `services::construction_payloads`, `ingress_start` is taken verbatim from the request with no lower-bound validation: [2](#0-1) 

The two guards are: [3](#0-2) 

Neither guard prevents `ingress_start` from being set to `1` (or any value far in the past). The while-loop then runs from `ingress_start` to `ingress_end` in steps of `ingress_interval - INGRESS_INTERVAL_OVERLAP`: [4](#0-3) 

`INGRESS_INTERVAL_OVERLAP` is 120 seconds: [5](#0-4) 

So the step size is `ingress_interval − 120s`. With `MAX_INGRESS_TTL = 300s` and `PERMITTED_DRIFT = 60s`, `ingress_interval = 240s`, giving a step of **120 seconds = 1.2 × 10¹¹ ns**.

With `ingress_start = 1` and `now ≈ 1.75 × 10¹⁸ ns` (year 2026), the loop runs approximately **14.6 million iterations**.

Each iteration calls `handle_construction_payloads`, which for every expiry clones `canister_method_args` and allocates a full `EnvelopeContent::Call` struct: [6](#0-5) 

The resulting `envelope_contents` and `signing_payloads` vectors grow to millions of entries before the function returns, causing OOM.

---

### Impact Explanation

The ICRC1 Rosetta server is a public-facing HTTP service with no authentication. A single malicious HTTP request causes the server process to allocate gigabytes of memory (millions of `EnvelopeContent::Call` structs, each containing a cloned `Vec<u8>` of canister method args), leading to OOM crash or severe memory pressure. This is a **denial-of-service** against the Rosetta API server, disrupting exchange integrations and any operator relying on the ICRC1 Rosetta service.

---

### Likelihood Explanation

The attack requires only a single unauthenticated HTTP POST to `/construction/payloads` with two crafted JSON fields. No credentials, keys, or special access are needed. It is trivially reproducible locally and requires no network-level attack.

---

### Recommendation

Add a lower-bound guard on `ingress_start` before the while-loop, for example:

```rust
if ingress_start + ingress_interval < now {
    return Err(Error::processing_construction_failed(&format!(
        "ingress_start is too far in the past: {ingress_start}"
    )));
}
```

Additionally, cap the maximum number of loop iterations (e.g., `MAX_INGRESS_EXPIRIES = 24 * 3600 / 120 = 720`) and return an error if the computed count would exceed it.

---

### Proof of Concept

```
POST /construction/payloads HTTP/1.1
Content-Type: application/json

{
  "network_identifier": { ... },
  "operations": [ <valid transfer operation> ],
  "public_keys": [ <valid public key> ],
  "metadata": {
    "ingress_start": 1,
    "ingress_end": <now_ns + 240_000_000_000>
  }
}
```

With `now_ns ≈ 1_750_000_000_000_000_000` (2026), the while-loop at lines 163–167 of `services.rs` runs ~14.6 million times. Each iteration allocates an `EnvelopeContent::Call` with a cloned `canister_method_args` blob. The process OOMs before returning a response.

### Citations

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
