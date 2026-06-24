### Title
`sender_info.sig` Included in `MessageId` Hash Enables Deduplication Bypass via Multiple Valid ICCSA Signatures — (`rs/types/types/src/messages/http.rs`, `rs/types/types/src/messages/ingress_messages.rs`)

---

### Summary

The `MessageId` for ingress messages is computed by hashing all fields of `sender_info` — including `info`, `signer`, **and `sig`**. Because ICCSA (canister signatures) are Merkle-proof-based and the same `info` bytes certified at two different subnet heights produce two cryptographically distinct but both-valid `sig` blobs, an attacker who controls a canister can submit two messages that are logically identical (same sender, canister_id, method_name, arg, ingress_expiry, nonce, info, signer) but carry different `sig` values. Both pass `validate_sender_info`, both receive distinct `MessageId`s, and both are inducted and executed — violating the replay-protection invariant.

---

### Finding Description

**Step 1 — `sig` is hashed into `MessageId`.**

`representation_independent_hash_call_or_query` in `rs/types/types/src/messages/http.rs` inserts the full `sender_info` map — `info`, `signer`, and `sig` — into the hash: [1](#0-0) 

`SignedIngressContent::id()` calls this function with `sender_info.sig` included: [2](#0-1) 

Two messages that differ only in `sender_info.sig` therefore produce different `MessageId`s.

**Step 2 — Deduplication is keyed on `MessageId`.**

`ValidSetRuleImpl::is_duplicate` checks the ingress history using `msg.content().id()`: [3](#0-2) 

`induct_messages` skips a message only if `is_duplicate` returns `true`: [4](#0-3) 

`IngressSetChain` / `IngressHistorySet` used during payload selection also key on `IngressMessageId` (which wraps `MessageId`): [5](#0-4) 

**Step 3 — `validate_sender_info` only checks cryptographic validity, not uniqueness of `sig`.**

`verify_sender_info_canister_sig` verifies that `sig` is a valid ICCSA signature over `SenderInfoContent(&info)`. It does not require `sig` to be the unique or canonical signature for those `info` bytes: [6](#0-5) 

**Step 4 — ICCSA signatures are non-unique by construction.**

An ICCSA signature is a Merkle proof that the canister's certified data contains a specific value. The same `info` bytes certified at two different subnet heights produce two different Merkle proofs — two different `sig` blobs — both of which verify correctly against the root of trust. An attacker who controls a canister can:

1. Have the canister certify `info` bytes at height H₁ → `sig₁`
2. Have the canister certify the same `info` bytes at height H₂ → `sig₂`
3. Submit M₁ (with `sig₁`) and M₂ (with `sig₂`) targeting the same canister, with identical (sender, canister_id, method_name, arg, ingress_expiry, nonce, info, signer)
4. Both pass `validate_sender_info` (both are valid ICCSA signatures)
5. `MessageId(M₁) ≠ MessageId(M₂)` because `sig₁ ≠ sig₂`
6. Neither is a duplicate of the other in the ingress history
7. Both are inducted and executed

---

### Impact Explanation

The same state-mutating call is executed twice against the target canister. Depending on the target, this can mean:
- Double token transfer / double spend
- Double execution of a privileged management operation
- Resource exhaustion (cycles drained twice)

The replay-protection invariant — that a message with a given (sender, canister_id, method_name, arg, ingress_expiry, nonce) is executed at most once — is broken.

---

### Likelihood Explanation

The attack requires the attacker to control a canister (accessible to any IC user; no privileged role needed) and to submit two messages within the same ingress expiry window (up to 5 minutes). Producing two valid ICCSA signatures for the same `info` bytes is straightforward: certify the value, wait one block, certify again. The attack is fully local-testable without any subnet-majority corruption or privileged access.

---

### Recommendation

Remove `sig` from the `MessageId` hash. The `sig` field is a proof of the `info` bytes, not part of the logical call identity. The `MessageId` should be computed from `(request_type, canister_id, method_name, arg, ingress_expiry, sender, nonce, sender_info.info, sender_info.signer)` — omitting `sender_info.sig`. The `sig` is already validated separately by `validate_sender_info` and does not need to contribute to message identity.

---

### Proof of Concept

```rust
// Pseudocode — both messages pass validate_sender_info and get distinct MessageIds

let info = b"user-attributes".to_vec();

// Canister C certifies `info` at height H1 → sig1
let sig1 = canister_c.certify_at_height(&info, H1);
// Canister C certifies `info` at height H2 → sig2 (different Merkle proof)
let sig2 = canister_c.certify_at_height(&info, H2);
assert_ne!(sig1, sig2); // different proofs, same content

let make_msg = |sig: Vec<u8>| SignedIngressContent::new_for_testing(
    sender, canister_id, method_name, arg, ingress_expiry, nonce,
    Some(SignedSenderInfo { info: info.clone(), signer: canister_c.id(), sig }),
);

let m1 = make_msg(sig1);
let m2 = make_msg(sig2);

// MessageIds differ because sig is hashed in
assert_ne!(m1.id(), m2.id());

// Both pass validate_sender_info (both are valid ICCSA signatures)
// Both are inducted — same logical call executed twice
valid_set_rule.induct_messages(&mut state, vec![m1.into(), m2.into()], round);
assert_eq!(execution_count_for(canister_id, method_name), 2); // replay!
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

**File:** rs/messaging/src/scheduling/valid_set_rule.rs (L349-357)
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
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L519-525)
```rust
        // Do not include the message if it's a duplicate.
        if past_ingress_set.contains(&ingress_id) {
            let message_id = MessageId::from(&ingress_id);
            return Err(ValidationError::InvalidArtifact(
                InvalidIngressPayloadReason::DuplicatedIngressMessage(message_id),
            ));
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
