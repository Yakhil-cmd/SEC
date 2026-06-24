Audit Report

## Title
Unbounded `ingress_end` Enables OOM DoS via Unbounded Envelope-Generation Loop — (`rs/rosetta-api/icrc1/src/construction_api/services.rs`)

## Summary
The `construction_payloads` function in the ICRC1 Rosetta API accepts an attacker-controlled `ingress_end` value with no upper bound check. With `ingress_end = u64::MAX`, the while-loop at lines 163–167 executes approximately 139 million iterations, allocating gigabytes of heap memory and crashing the Rosetta node. The same pattern exists in the ICP Rosetta implementation. The endpoint requires no authentication.

## Finding Description
`MAX_INGRESS_TTL` is 300 seconds and `PERMITTED_DRIFT` is 60 seconds, giving `ingress_interval = 240s = 240 × 10⁹ ns`. [1](#0-0) [2](#0-1) 

`INGRESS_INTERVAL_OVERLAP` is 120 seconds, so the loop step is `240s − 120s = 120s = 120 × 10⁹ ns`. [3](#0-2) 

The two guards before the loop are:
1. `if ingress_start >= ingress_end` — rejects if start is not before end.
2. `if ingress_end < now + ingress_interval` — rejects if end is too small (in the past or near-future). [4](#0-3) 

Neither guard imposes an **upper bound** on `ingress_end`. With `ingress_end = u64::MAX ≈ 1.844 × 10¹⁹ ns` and `ingress_start = now ≈ 1.75 × 10¹⁸ ns`, both guards pass. The loop then runs:

```
(u64::MAX − now) / (120 × 10⁹) ≈ 139,000,000 iterations
```

Each iteration pushes a `u64` into `ingress_expiries` (~1.1 GB for the vec alone). [5](#0-4) 

The entire `ingress_expiries` vec is then passed to `handle_construction_payloads`, which allocates a full `EnvelopeContent::Call` struct (containing `Vec<u8>` fields for `arg`, `nonce`, `method_name`, etc.) for every entry, multiplying memory consumption further. [6](#0-5) 

The endpoint is exposed with no authentication: [7](#0-6) 

The identical unbounded loop pattern exists in the ICP Rosetta implementation with the same step size (120s interval, no upper bound on `ingress_end`): [8](#0-7) 

## Impact Explanation
A single unauthenticated HTTP request causes the Rosetta process to exhaust available memory (OOM kill), making the node unavailable. This is a concrete application/platform-level DoS of the Rosetta API with direct user and protocol harm — all legitimate ledger transfers routed through that Rosetta node are blocked for the duration of the crash/restart cycle. This matches the **High ($2,000–$10,000)** impact class: "Significant Rosetta... security impact with concrete user or protocol harm."

## Likelihood Explanation
The endpoint requires no credentials. The malicious payload is a standard JSON body with one field set to a large integer (`u64::MAX`). Any attacker who can reach the Rosetta HTTP port can trigger this with a single `curl` command. The attack is trivially repeatable — the node can be crashed again immediately after restart.

## Recommendation
Add an explicit upper-bound check immediately after the existing guards, before the loop, in both `rs/rosetta-api/icrc1/src/construction_api/services.rs` and `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`:

```rust
const MAX_INGRESS_WINDOW: u64 = 24 * 60 * 60 * 1_000_000_000; // 24h in ns
if ingress_end > now + MAX_INGRESS_WINDOW {
    return Err(Error::processing_construction_failed(
        "ingress_end exceeds maximum allowed window"
    ));
}
```

This caps the loop to at most `24h / 120s = 720` iterations, which is safe.

## Proof of Concept
```rust
#[test]
fn test_construction_payloads_dos() {
    use std::time::{Duration, SystemTime};
    let now_sys = SystemTime::now();
    let now = now_sys
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .as_nanos() as u64;
    // ingress_start = now, ingress_end = u64::MAX
    // Guard 1: now < u64::MAX → passes
    // Guard 2: u64::MAX >= now + ingress_interval → passes
    // Loop runs ~139M iterations → OOM
    let result = construction_payloads(
        vec![/* valid transfer operation */],
        Some(ConstructionPayloadsRequestMetadata {
            ingress_start: Some(now),
            ingress_end: Some(u64::MAX),
            created_at_time: None,
            memo: None,
        }),
        &Principal::anonymous(),
        vec![/* valid public key */],
        now_sys,
    );
    assert!(result.is_err(), "must reject astronomically large ingress_end");
}
```

Without the fix, this test hangs or OOMs. With the recommended upper-bound guard, it returns `Err` immediately.

### Citations

**File:** rs/limits/src/lib.rs (L17-21)
```rust
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes

/// Duration subtracted from `MAX_INGRESS_TTL` by
/// `expiry_time_from_now()` when creating an ingress message.
pub const PERMITTED_DRIFT: Duration = Duration::from_secs(60);
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L120-121)
```rust
    let ingress_interval: u64 =
        (ic_limits::MAX_INGRESS_TTL - ic_limits::PERMITTED_DRIFT).as_nanos() as u64;
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
