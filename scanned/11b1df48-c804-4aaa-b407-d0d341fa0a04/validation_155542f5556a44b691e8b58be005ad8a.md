### Title
`SenderInfoContent` Canister Signature Not Bound to Request Context Enables Cross-Request Replay - (`rs/types/types/src/messages/http.rs`)

### Summary

The `sender_info` canister signature in IC ingress messages is verified only over the raw `info` bytes with domain `"ic-sender-info"`. It does not include the request sender principal, target canister ID, method name, argument, or ingress expiry. Any user sharing the same signing canister (e.g., Internet Identity) can replay a valid `sender_info` from one request in a completely different request, causing the receiving canister to observe a mismatched combination of `msg.caller()` (the attacker's principal) and `msg_caller_info_data()` (the victim's attested attributes).

### Finding Description

`SenderInfoContent` is defined as a thin wrapper over the raw `info` bytes:

```rust
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);
    }
}
``` [1](#0-0) 

The signed bytes are therefore `"ic-sender-info" || info_bytes` — nothing else. The `verify_sender_info_canister_sig` function verifies:

1. `sender_pubkey` is a valid canister signature public key.
2. The canister ID encoded in `sender_pubkey` matches `sender_info.signer`.
3. The canister signature over `SenderInfoContent(&sender_info.info)` is valid. [2](#0-1) 

None of these checks bind the `sender_info.sig` to: the request sender's principal, the target `canister_id`, the `method_name`, the `arg`, the `ingress_expiry`, or the `nonce`. The envelope-level signature (over the `MessageId`) does commit to the `sender_info` blob as a whole (all three fields: `info`, `signer`, `sig` are hashed into the message ID): [3](#0-2) 

But this only prevents the attacker from modifying the `sender_info` fields within a single request. It does not prevent replaying the entire `{info, signer, sig}` triple verbatim in a different request authored by a different sender.

**Attack path:**

1. Alice uses Internet Identity (II) as her identity. Her `sender_pubkey` is a canister signature public key from II canister.
2. Alice sends a request with `sender_info = {info: X, signer: II, sig: S}` where `S` is II's canister signature over `"ic-sender-info" || X`. `X` encodes Alice's verified attributes (e.g., `{user: "alice", role: "admin"}`).
3. Bob also uses Internet Identity (different seed/delegation, same II canister). Bob's `sender_pubkey` also encodes II's canister ID.
4. Bob observes Alice's request at the boundary node and extracts `{info: X, signer: II, sig: S}`.
5. Bob constructs his own request to any canister with the same `sender_info`. Bob signs his own message ID (which commits to his sender principal and Bob's `sender_info` copy).
6. Validation passes:
   - Bob's envelope signature is valid (he signed his own `MessageId`).
   - `pubkey_canister_id` (II) == `sender_info.signer` (II). ✓
   - Canister signature `S` over `SenderInfoContent(X)` is valid. ✓ [4](#0-3) 

The receiving canister now sees `msg.caller()` = Bob's principal, but `msg_caller_info_data()` = `X` (Alice's attested attributes). Any canister that uses `sender_info` for authorization (e.g., to grant elevated privileges based on verified attributes) is deceived.

### Impact Explanation

Any canister that reads `msg_caller_info_data()` and uses it for authorization or identity decisions receives a `sender_info` blob that was attested for a different request context. An attacker can present a valid `sender_info` originally issued for Alice's request in their own request, causing the canister to associate the attacker's principal with Alice's attested attributes. This enables privilege escalation, identity impersonation at the application layer, and bypassing attribute-based access control enforced by canisters.

### Likelihood Explanation

The attack requires only that the attacker and victim share the same signing canister (e.g., Internet Identity, which is the primary identity provider on IC). The attacker needs to observe a valid `sender_info` in transit — boundary nodes are the natural observation point. The `sender_info` feature is production-ready (system API `msg_caller_info_data()` exists, integration tests confirm acceptance of valid `sender_info`). Any canister that relies on `sender_info` for security decisions is immediately vulnerable. [5](#0-4) 

### Recommendation

Bind the `sender_info` canister signature to the request context. The signed content should include at minimum the sender principal and the target canister ID, so that a `sender_info` issued for one (sender, canister) pair cannot be replayed for a different pair. For example:

```rust
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub sender: &'a [u8],       // sender principal bytes
    pub canister_id: &'a [u8],  // target canister ID bytes
}

impl SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        // length-prefix each field to avoid ambiguity
        bytes.extend_from_slice(&(self.sender.len() as u32).to_be_bytes());
        bytes.extend_from_slice(self.sender);
        bytes.extend_from_slice(&(self.canister_id.len() as u32).to_be_bytes());
        bytes.extend_from_slice(self.canister_id);
        bytes.extend_from_slice(self.info);
    }
}
```

`verify_sender_info_canister_sig` must then extract the sender and canister ID from the request and pass them into the content construction, so the signature is verified against the full request-bound content. [6](#0-5) 

### Proof of Concept

```rust
// Alice (II user, seed_a) obtains a valid sender_info for info_bytes X:
let info_bytes = b"role=admin;user=alice".to_vec();
let sender_info_content = SenderInfoContent(&info_bytes);
let sig_alice = ii_canister_signer_alice.sign(&sender_info_content.as_signed_bytes()).await;
let alice_sender_info = RawSignedSenderInfo {
    info: Blob(info_bytes.clone()),
    signer: Blob(ii_canister_id.as_slice().to_vec()),
    sig: Blob(sig_alice),
};

// Bob (II user, seed_b) replays Alice's sender_info verbatim in his own request:
let bob_content = HttpCallContent::Call {
    update: HttpCanisterUpdate {
        canister_id: Blob(target_canister.as_slice().to_vec()),
        method_name: "privileged_action".to_string(),
        arg: Blob(vec![]),
        sender: Blob(bob_principal.as_slice().to_vec()), // Bob's principal
        ingress_expiry: expiry_time().as_nanos() as u64,
        nonce: None,
        sender_info: Some(alice_sender_info), // Alice's sender_info, unmodified
    },
};
// Bob signs his own message_id — validation passes.
// Target canister sees msg.caller() = Bob, msg_caller_info_data() = "role=admin;user=alice".
``` [7](#0-6) [8](#0-7)

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

**File:** rs/types/types/src/messages/http.rs (L341-348)
```rust
#[derive(Clone, Debug)]
pub struct SenderInfoContent<'a>(pub &'a [u8]);

impl crate::crypto::SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.0);
    }
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

**File:** rs/tests/crypto/ingress_verification_test.rs (L1207-1217)
```rust
            // The info blob that the signing canister attests to.
            let info_bytes = b"some user attributes".to_vec();
            let sender_info_content = SenderInfoContent(&info_bytes);
            let sender_info_signed_bytes = sender_info_content.as_signed_bytes();
            let sender_info_sig = signer.sign(&sender_info_signed_bytes).await;

            let valid_sender_info = || RawSignedSenderInfo {
                info: Blob(info_bytes.clone()),
                signer: Blob(signer.canister_id().get().as_slice().to_vec()),
                sig: Blob(sender_info_sig.clone()),
            };
```
