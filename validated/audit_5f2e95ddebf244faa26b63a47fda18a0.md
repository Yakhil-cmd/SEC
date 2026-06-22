### Title
Cross-Canister `sender_info` Signature Replay — (`rs/types/types/src/messages/http.rs`, `rs/validator/src/ingress_validation.rs`)

---

### Summary

The `sender_info` feature allows a canister (e.g., Internet Identity) to attest user attributes by signing an `info` blob with a canister signature. However, the signed digest (`SenderInfoContent`) contains only the raw `info` bytes and a domain separator — it does not include the target canister ID, the sender principal, or any request-specific nonce. This allows a valid `sender_info` signature to be replayed verbatim across requests to different canisters, as long as the same `info` bytes are presented.

---

### Finding Description

`SenderInfoContent` is defined as a thin wrapper over the raw `info` bytes: [1](#0-0) 

Its `SignatureDomain` implementation prepends only the literal string `"ic-sender-info"`: [2](#0-1) 

So the full signed byte string is:

```
\x0Eic-sender-info || info_bytes
```

No target canister ID, no sender principal, no nonce, no ingress expiry is included.

The verification path in `verify_sender_info_canister_sig` checks three things:

1. `sender_pubkey` is a valid canister-signature public key.
2. The canister ID encoded in `sender_pubkey` matches `sender_info.signer`.
3. The canister signature over `SenderInfoContent(&sender_info.info)` is valid. [3](#0-2) 

Critically, step 3 verifies only that the signing canister committed to `hash(info_bytes)` at path `sig/<seed>/<hash>` in its certified state tree. It does **not** verify that the commitment was made for a specific target canister or request. Because the canister signature is reusable for any request that presents the same `(signer_canister, seed, info_bytes)` triple, the same `sender_info` blob is accepted by the replica for requests directed at **any** canister.

---

### Impact Explanation

Any canister that reads `sender_info` via the `msg_caller_info_data` system API and uses it for access control (e.g., "caller has passed KYC", "caller is over 18 for canister X") can be bypassed by replaying a `sender_info` blob that was legitimately issued for a different canister. The attacker does not need to forge a signature — they reuse an existing, valid one. The replica accepts it without error because the protocol-level check does not bind the `sender_info` to the target canister. [4](#0-3) 

---

### Likelihood Explanation

The `sender_info` feature is new and actively being integrated. Any canister that relies on `sender_info` for per-canister access control without independently encoding the target canister ID inside the `info` blob is vulnerable. The attack requires only that the attacker has previously obtained a legitimate `sender_info` from the signing canister (e.g., by making a normal authenticated request to any canister that accepts `sender_info`). No privileged access, key compromise, or subnet-majority corruption is needed.

---

### Recommendation

Bind the `sender_info` signature to the target canister by including the target `canister_id` in the signed content. Modify `SenderInfoContent` and its `write_signed_bytes_without_domain_separator` implementation to incorporate the target canister ID:

```rust
pub struct SenderInfoContent<'a> {
    pub info: &'a [u8],
    pub target_canister_id: &'a CanisterId,
}

impl SignedBytesWithoutDomainSeparator for SenderInfoContent<'_> {
    fn write_signed_bytes_without_domain_separator(&self, bytes: &mut Vec<u8>) {
        bytes.extend_from_slice(self.target_canister_id.get_ref().as_slice());
        bytes.extend_from_slice(self.info);
    }
}
```

The `verify_sender_info_canister_sig` function must then pass the target canister ID from the request content when constructing `SenderInfoContent`. [5](#0-4) 

---

### Proof of Concept

1. User X sends a request to **canister A** with a valid `sender_info` blob `{info: b"user-has-kyc", signer: II_canister_id, sig: <valid_sig>}`. The replica accepts it.
2. User X constructs a second request to **canister B** (a different canister), reusing the identical `sender_info` blob unchanged.
3. The replica validates the second request. `verify_sender_info_canister_sig` checks that the canister signature over `\x0Eic-sender-info || b"user-has-kyc"` is valid — it is, because the signed bytes are identical. The check passes.
4. Canister B receives the call with `msg_caller_info_data()` returning `b"user-has-kyc"`, even though II never attested this for canister B.

The root cause is that steps 1 and 2 produce identical `SenderInfoContent` signed bytes regardless of the target canister, so the same canister signature satisfies both verifications. [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/types/types/src/crypto/sign.rs (L161-165)
```rust
impl<'a> SignatureDomain for SenderInfoContent<'a> {
    fn domain(&self) -> Vec<u8> {
        domain_with_prepended_length("ic-sender-info")
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

**File:** rs/execution_environment/src/execution/nonreplicated_query.rs (L1-5)
```rust
// This module defines how non-replicated query messages are executed.
// See https://internetcomputer.org/docs/interface-spec/index.html#http-query
//
// Note that execution of replicated queries (queries in the update context)
// is defined in the `call` module.
```
