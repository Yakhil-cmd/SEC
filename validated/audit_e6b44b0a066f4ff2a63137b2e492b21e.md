Let me look at the ingress pool admission and the `IngressSetChain` to complete the picture.

### Title
`sender_info.sig` Included in `MessageId` Hash Enables Replay of State-Mutating Calls via Non-Unique Canister Signatures — (`rs/types/types/src/messages/http.rs`, `rs/types/types/src/messages/ingress_messages.rs`)

---

### Summary

The `sender_info.sig` field (a canister signature witness) is included in the `MessageId` hash. Because IC canister signatures are non-unique — the same content can be signed at different block heights, producing different but equally valid CBOR witnesses — an attacker who controls a canister can produce two structurally distinct but semantically equivalent `sender_info.sig` values for the same `info_bytes`. This yields two different `MessageId`s for the same logical call, both of which pass `validate_sender_info` and both of which bypass `ValidSetRuleImpl::is_duplicate`, allowing the same state-mutating ingress call to be inducted and executed twice.

---

### Finding Description

**Step 1 — `sig` is hashed into `MessageId`.**

`representation_independent_hash_call_or_query` in `rs/types/types/src/messages/http.rs` inserts the full `sender_info` map — including `sig` — into the hash: [1](#0-0) 

`SignedIngressContent::id()` calls this function directly: [2](#0-1) 

Two messages identical in every field except `sender_info.sig` therefore produce different `MessageId`s.

**Step 2 — Deduplication is keyed solely on `MessageId`.**

`ValidSetRuleImpl::is_duplicate` checks only the ingress history keyed by `MessageId`: [3](#0-2) 

`induct_messages` skips a message only when `is_duplicate` returns `true`: [4](#0-3) 

`IngressSetChain::contains` and `IngressHistorySet::contains` also key on `IngressMessageId` (which embeds `MessageId`): [5](#0-4) 

**Step 3 — Canister signatures are non-unique.**

A canister signature is a CBOR `{certificate, tree}` where `certificate` is a subnet BLS certification of the canister's certified data at a specific block height. The same `info_bytes` certified at two different rounds produces two different witnesses (different Merkle proofs from different state-tree roots), both of which pass `verify_sender_info_canister_sig`: [6](#0-5) 

The `CanisterSigner::sign` implementation in the test suite confirms this: each call to `certify_variable` fetches a fresh certificate from the live subnet, so two calls at different heights yield different CBOR blobs: [7](#0-6) 

**Step 4 — `validate_sender_info` is called before induction, not before pool admission.**

`validate_request_content` validates the envelope signature and `sender_info` at the HTTP boundary: [8](#0-7) 

Both messages pass this check independently. There is no cross-message deduplication at the validation layer that would detect two messages with the same `info`/`signer` but different `sig`.

---

### Impact Explanation

An attacker who controls a canister C can:

1. Have C certify `hash(SenderInfoContent(info_bytes))` at round R1 → witness W1.
2. Have C certify the same data at round R2 → witness W2 (W1 ≠ W2).
3. Craft two ingress messages with identical `(sender, canister_id, method_name, arg, ingress_expiry, nonce)` but `sender_info.sig = W1` vs `sender_info.sig = W2`.
4. Produce valid envelope canister signatures over `MessageId1` and `MessageId2` respectively (C can certify any content).
5. Submit both. Both pass `validate_sender_info`. Both have distinct `MessageId`s. Both are inducted and executed.

The same state-mutating call executes twice, violating the replay-protection invariant. Concrete impact: double-spending tokens, double-executing privileged operations, draining cycles from a target canister.

---

### Likelihood Explanation

- Requires only a deployed canister (permissionless on the IC).
- No threshold corruption, no admin key, no social engineering.
- Two sequential `certified_data_set` + `data_certificate` calls suffice to obtain W1 and W2.
- Fully local-testable with the existing `CanisterSigner` test harness.

---

### Recommendation

Remove `sig` from the `MessageId` hash. Only `info` and `signer` are semantically meaningful for message identity; `sig` is an authentication proof that should be validated but must not affect deduplication. Change `representation_independent_hash_call_or_query` to omit `sig` from the `sender_info` sub-map:

```rust
map.insert(
    "sender_info",
    Map(btreemap! {
        "info"   => Bytes(info),
        "signer" => Bytes(signer),
        // sig intentionally excluded from MessageId
    }),
);
``` [1](#0-0) 

This aligns with the `ic_agent` library's existing behavior (which already excludes `sender_info` from the request ID computation, as noted in the test comment at line 1220–1221 of `rs/tests/crypto/ingress_verification_test.rs`).

---

### Proof of Concept

```rust
// Pseudocode — uses existing CanisterSigner test harness
let signer = CanisterSigner::new(&canister, seed);
let info_bytes = b"user-attributes".to_vec();
let sender_info_content = SenderInfoContent(&info_bytes);

// Two certifications at different rounds → different witnesses
let sig1 = signer.sign(&sender_info_content.as_signed_bytes()).await; // round R1
let sig2 = signer.sign(&sender_info_content.as_signed_bytes()).await; // round R2
assert_ne!(sig1, sig2); // different CBOR witnesses

let make_msg = |sig: Vec<u8>| HttpCanisterUpdate {
    sender_info: Some(RawSignedSenderInfo {
        info: Blob(info_bytes.clone()),
        signer: Blob(signer.canister_id().get().as_slice().to_vec()),
        sig: Blob(sig),
    }),
    // all other fields identical
    ..base_update.clone()
};

let msg1 = make_msg(sig1);
let msg2 = make_msg(sig2);

// MessageIds differ because sig is hashed in
assert_ne!(msg1.id(), msg2.id());

// Both pass validate_request → both inducted → executed twice
valid_set_rule.induct_messages(&mut state, vec![msg1.into(), msg2.into()], round);
assert_eq!(execution_count, 2); // replay-protection violated
```

### Citations

**File:** rs/types/types/src/messages/http.rs (L68-77)
```rust
    if let Some(RawSignedSenderInfoSlices { info, signer, sig }) = sender_info {
        map.insert(
            "sender_info",
            Map(btreemap! {
                "info" => Bytes(info),
                "signer" => Bytes(signer),
                "sig" => Bytes(sig),
            }),
        );
    }
```

**File:** rs/types/types/src/messages/ingress_messages.rs (L113-130)
```rust
    fn id(&self) -> MessageId {
        MessageId::from(representation_independent_hash_call_or_query(
            CallOrQuery::Call,
            self.canister_id.as_ref(),
            &self.method_name,
            &self.arg,
            self.ingress_expiry,
            self.sender.get_ref().as_slice(),
            self.nonce.as_deref(),
            self.sender_info
                .as_ref()
                .map(|sender_info| RawSignedSenderInfoSlices {
                    info: &sender_info.info,
                    signer: sender_info.signer.as_ref(),
                    sig: &sender_info.sig,
                }),
        ))
    }
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L206-208)
```rust
    fn is_duplicate(&self, state: &ReplicatedState, msg: &SignedIngress) -> bool {
        state.get_ingress_status(&msg.content().id()) != &IngressStatus::Unknown
    }
```

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L349-358)
```rust
        for msg in msgs {
            let message_id = msg.content().id();
            if !self.is_duplicate(state, &msg) {
                self.induct_message(state, msg, current_round);
            } else {
                self.observe_inducted_ingress_status(LABEL_VALUE_DUPLICATE);
                debug!(self.log, "Didn't induct duplicate message {}", message_id);
            }
        }
        self.observe_ingress_history_size(state.total_ingress_memory_taken());
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L740-750)
```rust
impl<T: IngressSetQuery> IngressSetQuery for IngressSetChain<'_, T> {
    fn contains(&self, msg_id: &IngressMessageId) -> bool {
        if self.first.contains(msg_id) {
            true
        } else {
            self.next
                .as_ref()
                .map(|set| set.contains(msg_id))
                .unwrap_or(false)
        }
    }
```

**File:** rs/validator/src/ingress_validation.rs (L196-221)
```rust
fn validate_request_content<C: HttpRequestContent, R: RootOfTrustProvider>(
    request: &HttpRequest<C>,
    ingress_signature_verifier: &dyn IngressSigVerifier,
    current_time: Time,
    root_of_trust_provider: &R,
) -> Result<CanisterIdSet, RequestValidationError>
where
    R::Error: std::error::Error,
{
    validate_nonce(request)?;
    // Validate the envelope signature first (cheap check) before performing
    // expensive canister signature verification in validate_sender_info.
    let targets = validate_user_id_and_signature(
        ingress_signature_verifier,
        &request.sender(),
        &request.id(),
        match request.authentication() {
            Authentication::Anonymous => None,
            Authentication::Authenticated(signature) => Some(signature),
        },
        current_time,
        root_of_trust_provider,
    )?;
    validate_sender_info(request, ingress_signature_verifier, root_of_trust_provider)?;
    Ok(targets)
}
```

**File:** rs/validator/src/ingress_validation.rs (L494-544)
```rust
fn verify_sender_info_canister_sig<R: RootOfTrustProvider>(
    sender_info: &SignedSenderInfo,
    sender_pubkey_bytes: &[u8],
    validator: &dyn IngressSigVerifier,
    root_of_trust_provider: &R,
) -> Result<(), RequestValidationError>
where
    R::Error: std::error::Error,
{
    // Parse the envelope-level sender_pubkey DER to extract the raw
    // public key bytes and verify it's a valid canister signature public key.
    let pk_bytes = public_key_bytes_from_der(sender_pubkey_bytes).map_err(|e| {
        InvalidSenderInfo(format!(
            "sender_pubkey is not a valid canister signature public key: {e}"
        ))
    })?;

    // Extract the canister ID from the parsed public key and verify
    // it matches the declared signer.
    let parsed_pk = ic_crypto_iccsa::types::PublicKey::try_from(&pk_bytes)
        .map_err(|e| InvalidSenderInfo(format!("invalid canister sig public key: {e:?}")))?;
    let pubkey_canister_id = parsed_pk.signing_canister_id();
    if pubkey_canister_id != sender_info.signer {
        return Err(InvalidSenderInfo(format!(
            "signer {} does not match canister ID {} in sender_pubkey",
            sender_info.signer, pubkey_canister_id
        )));
    }

    // Construct the UserPublicKey for verification.
    let public_key = UserPublicKey {
        key: pk_bytes.0,
        algorithm_id: AlgorithmId::IcCanisterSignature,
    };

    // Construct the signable content (domain = "ic-sender-info")
    let sender_info_content = SenderInfoContent(&sender_info.info);
    let canister_sig = CanisterSigOf::from(CanisterSig(sender_info.sig.clone()));

    verify_canister_sig_with_fallback!(
        validator,
        &canister_sig,
        &sender_info_content,
        &public_key,
        root_of_trust_provider,
        |e| InvalidSenderInfo(format!("signature verification failed: {e}")),
        |e: <R as RootOfTrustProvider>::Error| InvalidSenderInfo(format!(
            "failed to get root of trust: {e}"
        ))
    );
    Ok(())
```

**File:** rs/tests/crypto/ingress_verification_test.rs (L324-370)
```rust
    pub async fn sign(&self, message: &[u8]) -> Vec<u8> {
        use ic_certification::{HashTree, labeled, leaf};
        use ic_crypto_sha2::Sha256;
        use serde::Serialize;
        use serde_bytes::ByteBuf;

        let seed_hash = Sha256::hash(&self.seed);
        let msg_hash = Sha256::hash(message);
        let sig_tree = labeled(b"sig", labeled(seed_hash, labeled(msg_hash, leaf(b""))));

        let mut certificate_cbor = self.certify_variable(&sig_tree.digest()).await;

        if let Some(rng_seed) = self.random_certificate_signature_rng_seed {
            let rng = &mut StdRng::from_seed(rng_seed);
            certificate_cbor = resign_certificate_with_random_signature(&certificate_cbor, rng);
        }

        #[derive(serde::Serialize)]
        struct CanisterSignature {
            certificate: ByteBuf,
            tree: HashTree,
        }
        let canister_sig = CanisterSignature {
            certificate: ByteBuf::from(certificate_cbor),
            tree: sig_tree,
        };
        // serialize to self-describing CBOR
        let mut serializer = serde_cbor::Serializer::new(Vec::new());
        serializer.self_describe().unwrap();
        canister_sig.serialize(&mut serializer).unwrap();
        serializer.into_inner()
    }

    async fn certify_variable(&self, variable_data: &[u8]) -> Vec<u8> {
        use ic_universal_canister::wasm;

        let _ = self
            .canister
            .update(wasm().certified_data_set(variable_data).reply().build())
            .await
            .expect("failed to call universal canister to set certified data");

        self.canister
            .query(wasm().data_certificate().append_and_reply().build())
            .await
            .expect("failed to call universal canister to get data certificate")
    }
```
