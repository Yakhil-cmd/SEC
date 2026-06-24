Now I have all the evidence needed. Let me trace the full exploit path precisely.

**Step 1 — Missing guard in ICP Rosetta `construction_payloads`:**

The ICP Rosetta handler has no check for `ingress_start >= ingress_end`. The loop at lines 99–107 is simply `while now < ingress_end`, so when `ingress_start == ingress_end`, the loop body never executes and `ingress_expiries` remains `vec![]`. [1](#0-0) 

Compare with ICRC1 Rosetta, which has an explicit guard rejecting this case: [2](#0-1) 

**Step 2 — `add_payloads` produces zero `SigningPayload`s, but `updates.push` still fires:**

`add_payloads` iterates over `ingress_expiries`; with an empty slice it adds nothing to `payloads`. However, `updates.push((RequestType::Send, update))` is unconditional, so `UnsignedTransaction { updates: [(Send, update)], ingress_expiries: [] }` is returned to the client. [3](#0-2) [4](#0-3) 

**Step 3 — `construction_combine` produces `SignedTransaction` with empty `EnvelopePair` vecs:**

The inner loop over `ingress_expiries` never executes, so `request_envelopes` stays `vec![]`. The outer loop still pushes `(request_type, vec![])` into `requests`. [5](#0-4) 

The resulting `SignedTransaction` is `{ requests: [(RequestType::Send, [])] }`. [6](#0-5) 

**Step 4 — `construction_parse` with `signed=true` panics at `updates[0]`:**

The signed branch maps over `signed_transaction.requests` and unconditionally indexes `updates[0]`. With an empty `Vec<EnvelopePair>`, this is an index-out-of-bounds panic. [7](#0-6) 

---

### Title
Missing ingress window validation in ICP Rosetta `/construction/payloads` causes panic at `updates[0]` in `/construction/parse` — (`rs/rosetta-api/icp/src/request_handler/construction_parse.rs`)

### Summary
The ICP Rosetta API server does not validate that `ingress_start < ingress_end` in `/construction/payloads`. Supplying `ingress_start == ingress_end` produces an `UnsignedTransaction` with an empty `ingress_expiries` vec. This propagates through `/construction/combine` (which produces a `SignedTransaction` with empty `EnvelopePair` vecs per request), and then causes an index-out-of-bounds panic at `updates[0]` in `/construction/parse` when called with `signed=true`.

### Finding Description
In `construction_payloads.rs`, the ingress expiry window loop is:

```rust
let mut now = ingress_start;
while now < ingress_end {          // never executes when start == end
    ingress_expiries.push(...);
    now += interval;
}
```

No guard rejects `ingress_start >= ingress_end`. The ICRC1 Rosetta counterpart has an explicit rejection at `services.rs:148–152`, but the ICP Rosetta handler has no equivalent.

`add_payloads` iterates over the empty `ingress_expiries` slice and produces zero `SigningPayload`s. However, `updates.push(...)` is unconditional, so the `UnsignedTransaction` carries one update entry with zero expiries.

In `construction_combine`, the inner loop over `ingress_expiries` never fires, so `request_envelopes` is `vec![]`. The outer loop still pushes `(request_type, vec![])` into `requests`, producing a structurally valid but semantically broken `SignedTransaction`.

In `construction_parse` with `signed=true`, line 42 unconditionally indexes `updates[0]` on the `Vec<EnvelopePair>` for each request. With an empty vec, this is an index-out-of-bounds panic.

### Impact Explanation
An unprivileged client can crash the ICP Rosetta API server process (or at minimum abort the request handler thread, depending on whether the web framework catches panics) with a three-step, non-volumetric call sequence. No authentication, no privileged role, and no prior state is required. The IC consensus layer and replicas are unaffected; the impact is a DoS against the Rosetta service process.

### Likelihood Explanation
The call sequence is straightforward and requires only crafting a `ConstructionPayloadsRequest` with `ingress_start == ingress_end`. The ICRC1 Rosetta already has the fix, demonstrating the pattern is known. Any client that discovers the missing guard can trigger this deterministically.

### Recommendation
Add the same guard present in the ICRC1 Rosetta handler at the top of `construction_payloads` in `rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`:

```rust
if ingress_start >= ingress_end {
    return Err(ApiError::invalid_request(format!(
        "ingress_start must be before ingress_end: start={}, end={}",
        ingress_start.as_nanos_since_unix_epoch(),
        ingress_end.as_nanos_since_unix_epoch()
    )));
}
```

Additionally, add a defensive check in `construction_parse` before indexing `updates[0]`, and consider asserting non-empty `ingress_expiries` before serializing the `UnsignedTransaction`.

### Proof of Concept
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

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L41-171)
```rust
        for (request_type, update) in unsigned_transaction.updates {
            let mut request_envelopes = vec![];

            for ingress_expiry in &unsigned_transaction.ingress_expiries {
                let mut update = update.clone();
                update.ingress_expiry = *ingress_expiry;

                let read_state = make_read_state_from_update(&update);

                let transaction_signature = signatures_by_sig_data
                    .get(&make_sig_data(&update.id()))
                    .ok_or_else(|| {
                        ApiError::internal_error(
                            "Could not find signature for transaction".to_string(),
                        )
                    })?;
                let read_state_signature = signatures_by_sig_data
                    .get(&make_sig_data(&MessageId::from(
                        read_state.representation_independent_hash(),
                    )))
                    .ok_or_else(|| {
                        ApiError::internal_error(
                            "Could not find signature for read-state".to_string(),
                        )
                    })?;
                let envelope = match transaction_signature.signature_type {
                    SignatureType::Ed25519 => Ok(HttpRequestEnvelope::<HttpCallContent> {
                        content: HttpCallContent::Call { update },
                        sender_pubkey: Some(Blob(
                            Ed25519KeyPair::der_encode_pk(
                                Ed25519KeyPair::hex_decode_pk(
                                    &transaction_signature.public_key.hex_bytes,
                                )
                                .map_err(|err| {
                                    ApiError::InvalidPublicKey(
                                        false,
                                        Details::from(format!("{err:?}")),
                                    )
                                })?,
                            )
                            .map_err(|err| {
                                ApiError::InvalidPublicKey(false, Details::from(format!("{err:?}")))
                            })?,
                        )),
                        sender_sig: Some(Blob(from_hex(&transaction_signature.hex_bytes)?)),
                        sender_delegation: None,
                    }),
                    SignatureType::Ecdsa => Ok(HttpRequestEnvelope::<HttpCallContent> {
                        content: HttpCallContent::Call { update },
                        sender_pubkey: Some(Blob(
                            Secp256k1KeyPair::der_encode_pk(
                                Secp256k1KeyPair::hex_decode_pk(
                                    &transaction_signature.public_key.hex_bytes,
                                )
                                .map_err(|err| {
                                    ApiError::InvalidPublicKey(
                                        false,
                                        Details::from(format!("{err:?}")),
                                    )
                                })?,
                            )
                            .map_err(|err| {
                                ApiError::InvalidPublicKey(false, Details::from(format!("{err:?}")))
                            })?,
                        )),
                        sender_sig: Some(Blob(from_hex(&transaction_signature.hex_bytes)?)),
                        sender_delegation: None,
                    }),
                    sig_type => Err(ApiError::InvalidRequest(
                        false,
                        format!("Sginature Type {sig_type} not supported byt rosetta").into(),
                    )),
                }?;

                let read_state_envelope = match read_state_signature.signature_type {
                    SignatureType::Ed25519 => Ok(HttpRequestEnvelope::<HttpReadStateContent> {
                        content: HttpReadStateContent::ReadState { read_state },
                        sender_pubkey: Some(Blob(
                            Ed25519KeyPair::der_encode_pk(
                                Ed25519KeyPair::hex_decode_pk(
                                    &read_state_signature.public_key.hex_bytes,
                                )
                                .map_err(|err| {
                                    ApiError::InvalidPublicKey(
                                        false,
                                        Details::from(format!("{err:?}")),
                                    )
                                })?,
                            )
                            .map_err(|err| {
                                ApiError::InvalidPublicKey(false, Details::from(format!("{err:?}")))
                            })?,
                        )),
                        sender_sig: Some(Blob(from_hex(&read_state_signature.hex_bytes)?)),
                        sender_delegation: None,
                    }),
                    SignatureType::Ecdsa => Ok(HttpRequestEnvelope::<HttpReadStateContent> {
                        content: HttpReadStateContent::ReadState { read_state },
                        sender_pubkey: Some(Blob(
                            Secp256k1KeyPair::der_encode_pk(
                                Secp256k1KeyPair::hex_decode_pk(
                                    &transaction_signature.public_key.hex_bytes,
                                )
                                .map_err(|err| {
                                    ApiError::InvalidPublicKey(
                                        false,
                                        Details::from(format!("{err:?}")),
                                    )
                                })?,
                            )
                            .map_err(|err| {
                                ApiError::InvalidPublicKey(false, Details::from(format!("{err:?}")))
                            })?,
                        )),

                        sender_sig: Some(Blob(from_hex(&read_state_signature.hex_bytes)?)),
                        sender_delegation: None,
                    }),
                    sig_type => Err(ApiError::InvalidRequest(
                        false,
                        format!("Sginature Type {sig_type} not supported byt rosetta").into(),
                    )),
                }?;
                request_envelopes.push(EnvelopePair {
                    update: envelope,
                    read_state: read_state_envelope,
                });
            }

            requests.push((request_type, request_envelopes));
        }
```

**File:** rs/rosetta-api/icp/src/models.rs (L32-58)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Deserialize, Serialize)]
pub struct SignedTransaction {
    pub requests: Vec<Request>,
}

impl FromStr for SignedTransaction {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let bytes = hex::decode(s).map_err(|err| format!("{err:?}"))?;
        serde_cbor::from_slice(bytes.as_slice()).or_else(|first_err| {
            serde_cbor::from_slice::<LegacySignedTransaction>(bytes.as_slice())
                .map(|legacy_requests| SignedTransaction {
                    requests: legacy_requests,
                })
                .map_err(|_| format!("{first_err:?}"))
        })
    }
}
impl std::fmt::Display for SignedTransaction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", hex::encode(serde_cbor::to_vec(self).unwrap()))
    }
}

/// A vector of update/read-state calls for different ingress windows
/// of the same call.
pub type Request = (RequestType, Vec<EnvelopePair>);
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
