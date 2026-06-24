Audit Report

## Title
Missing ingress window validation in ICP Rosetta `/construction/payloads` causes index-out-of-bounds panic in `/construction/parse` — (`rs/rosetta-api/icp/src/request_handler/construction_parse.rs`)

## Summary
The ICP Rosetta API does not validate that `ingress_start < ingress_end` in `/construction/payloads`. Supplying `ingress_start == ingress_end` produces an `UnsignedTransaction` with an empty `ingress_expiries` vec and zero `SigningPayload`s. This propagates through `/construction/combine`, which produces a `SignedTransaction` with an empty `Vec<EnvelopePair>` per request. A subsequent call to `/construction/parse` with `signed=true` unconditionally indexes `updates[0]` on that empty vec, causing an index-out-of-bounds panic and crashing the request handler.

## Finding Description
**Root cause — missing guard in `construction_payloads.rs`:**

The ingress expiry loop at lines 99–107 is `while now < ingress_end`. When `ingress_start == ingress_end`, the loop body never executes and `ingress_expiries` remains `vec![]`. No prior guard rejects this case. [1](#0-0) 

The ICRC1 Rosetta counterpart has an explicit rejection at `services.rs:148–152`, but the ICP Rosetta handler has no equivalent. [2](#0-1) 

**Propagation through `add_payloads` and `updates.push`:**

`add_payloads` iterates over the empty `ingress_expiries` slice and adds nothing to `payloads`. However, `updates.push((RequestType::Send, update))` at line 377 is unconditional, so the returned `UnsignedTransaction` carries one update entry with zero expiries and zero signing payloads. [3](#0-2) [4](#0-3) 

**Propagation through `construction_combine`:**

The inner loop `for ingress_expiry in &unsigned_transaction.ingress_expiries` never executes, so `request_envelopes` stays `vec![]`. The outer loop still unconditionally pushes `(request_type, vec![])` into `requests` at line 170, producing `SignedTransaction { requests: [(Send, [])] }`. Because there are zero payloads, zero signatures are required, so `construction_combine` accepts `"signatures": []` without error. [5](#0-4) [6](#0-5) 

**Panic in `construction_parse`:**

The signed branch at line 42 unconditionally indexes `updates[0]` on the `Vec<EnvelopePair>` for each request. With an empty vec, this is an index-out-of-bounds panic. [7](#0-6) 

## Impact Explanation
An unprivileged external client can crash the ICP Rosetta API server process (or abort the request handler thread) with a deterministic, non-volumetric three-step call sequence requiring no authentication, no privileged role, and no prior state. This constitutes an application/platform-level DoS against the Rosetta service, matching the **High** impact category: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."* The IC consensus layer and replicas are unaffected; the impact is confined to the Rosetta service process.

## Likelihood Explanation
The attack requires only crafting a `ConstructionPayloadsRequest` with `ingress_start == ingress_end` in the metadata. The call sequence is fully deterministic and requires no special knowledge beyond the public Rosetta API specification. The ICRC1 Rosetta already contains the fix, demonstrating the pattern is known. Any client that discovers the missing guard can trigger this reliably and repeatedly.

## Recommendation
Add the same guard present in the ICRC1 Rosetta handler at the top of the ingress window computation in `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request(format!(
        "ingress_start must be before ingress_end: start={}, end={}",
        ingress_start.as_nanos_since_unix_epoch(),
        ingress_end.as_nanos_since_unix_epoch()
    )));
}
```

Additionally, add a defensive bounds check in `construction_parse.rs` before indexing `updates[0]`, and consider asserting non-empty `ingress_expiries` before serializing the `UnsignedTransaction`.

## Proof of Concept
```
POST /construction/payloads
{
  "network_identifier": ...,
  "operations": [<valid Transfer operation>],
  "public_keys": [<valid public key>],
  "metadata": { "ingress_start": T, "ingress_end": T }   // start == end
}
→ Response: unsigned_transaction with ingress_expiries:[], payloads:[]

POST /construction/combine
{
  "network_identifier": ...,
  "unsigned_transaction": <above>,
  "signatures": []   // zero payloads → zero signatures required
}
→ Response: signed_transaction with requests:[(Send, [])]

POST /construction/parse
{
  "network_identifier": ...,
  "signed": true,
  "transaction": <above signed_transaction>
}
→ PANIC: index out of bounds: the len is 0 but the index is 0
   at construction_parse.rs:42  updates[0]
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

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L370-378)
```rust
    add_payloads(
        payloads,
        ingress_expiries,
        &convert::to_model_account_identifier(&from),
        &update,
        SignatureType::from(pk.curve_type),
    );
    updates.push((RequestType::Send, update));
    Ok(())
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L1048-1076)
```rust
fn add_payloads(
    payloads: &mut Vec<SigningPayload>,
    ingress_expiries: &[u64],
    account_identifier: &AccountIdentifier,
    update: &HttpCanisterUpdate,
    signature_type: SignatureType,
) {
    for ingress_expiry in ingress_expiries {
        let mut update = update.clone();
        update.ingress_expiry = *ingress_expiry;
        let message_id = update.id();
        let transaction_payload = SigningPayload {
            address: None,
            account_identifier: Some(account_identifier.clone()),
            hex_bytes: hex::encode(make_sig_data(&message_id)),
            signature_type: Some(signature_type),
        };
        payloads.push(transaction_payload);
        let read_state = make_read_state_from_update(&update);
        let read_state_message_id = MessageId::from(read_state.representation_independent_hash());
        let read_state_payload = SigningPayload {
            address: None,
            account_identifier: Some(account_identifier.clone()),
            hex_bytes: hex::encode(make_sig_data(&read_state_message_id)),
            signature_type: Some(signature_type),
        };
        payloads.push(read_state_payload);
    }
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L148-152)
```rust
    if ingress_start >= ingress_end {
        return Err(Error::processing_construction_failed(&format!(
            "Ingress start should start before ingress end: Start: {ingress_start}, End: {ingress_end}"
        )));
    }
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L41-44)
```rust
        for (request_type, update) in unsigned_transaction.updates {
            let mut request_envelopes = vec![];

            for ingress_expiry in &unsigned_transaction.ingress_expiries {
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L164-171)
```rust
                request_envelopes.push(EnvelopePair {
                    update: envelope,
                    read_state: read_state_envelope,
                });
            }

            requests.push((request_type, request_envelopes));
        }
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_parse.rs (L37-46)
```rust
        let updates: Vec<_> = match ParsedTransaction::try_from(msg.clone())? {
            ParsedTransaction::Signed(signed_transaction) => signed_transaction
                .requests
                .iter()
                .map(
                    |(request_type, updates)| match updates[0].update.content.clone() {
                        HttpCallContent::Call { update } => (request_type.clone(), update),
                    },
                )
                .collect(),
```
