### Title
`sender_info` Canister Signature Has No Expiration — Issued Attestations Are Irrevocable - (File: `rs/validator/src/ingress_validation.rs`)

### Summary

The `sender_info` feature allows a canister (e.g., Internet Identity) to attach a signed attestation blob to any ingress or query request. The canister signature over the `info` bytes contains no expiration timestamp. Once a signing canister issues a `sender_info` signature, the recipient user can replay that same signature in new requests indefinitely. The only way to revoke it is to change the canister's signing key material (seed), which invalidates all previously issued attestations — a blunt, all-or-nothing instrument identical in structure to the NftPort finding.

### Finding Description

`SignedSenderInfo` is defined with three fields: `info`, `signer`, and `sig`. [1](#0-0) 

The signable content (`SenderInfoContent`) is constructed from only the raw `info` bytes, with domain separator `"ic-sender-info"`: [2](#0-1) 

The validation function `verify_sender_info_canister_sig` checks three things: (1) the sender pubkey is a valid canister signature public key, (2) the canister ID in the pubkey matches the declared signer, and (3) the signature over the `info` blob is cryptographically valid. There is no expiration field in the signed content and no expiration check in the validator: [3](#0-2) 

Compare this to how `sender_delegation` expiry is enforced — delegations carry an explicit `expiration` field that is checked against `current_time`: [4](#0-3) 

The `sender_info.sig` is a standalone canister signature over `"ic-sender-info" || info_bytes`. It is not bound to any specific message, request ID, or time window. The outer `ingress_expiry` limits how long a single signed *message* can be submitted, but the `sender_info.sig` credential itself is reusable across an unlimited number of new messages constructed by the user.

### Impact Explanation

A canister acting as an attribute provider (e.g., Internet Identity attesting "user has admin role" or "user is KYC-verified") cannot revoke a `sender_info` attestation it has already issued. The user retains a valid, forever-reusable credential. The receiving canister, reading the info via the `msg_caller_info_data` system API, has no way to distinguish a freshly issued attestation from one that was revoked by the signer months ago. The only remediation available to the signing canister is to rotate its canister signature seed, which invalidates every attestation it has ever issued — an all-or-nothing nuclear option, not targeted revocation.

### Likelihood Explanation

The `sender_info` mechanism is explicitly designed for use by identity/attribute providers such as Internet Identity. Any deployment where a canister issues time-limited or revocable credentials (e.g., session tokens, role grants, KYC status) via `sender_info` is immediately affected. An unprivileged user who legitimately received a `sender_info` signature and later has their access revoked can continue presenting the old signature in new requests. No special privileges or key compromise are required — the user simply reuses the bytes they already hold.

### Recommendation

Add an expiration timestamp to the signed content. The `SenderInfoContent` should include a `expiry_ns: u64` (nanoseconds since Unix epoch) field alongside the `info` bytes, so the signed payload becomes `"ic-sender-info" || expiry_ns || info_bytes`. The `RawSignedSenderInfo` / `SignedSenderInfo` structs should expose this field, and `verify_sender_info_canister_sig` should check `expiry_ns >= current_time` before accepting the signature, analogous to how `validate_sender_delegation_expiry` enforces delegation expiry. [5](#0-4) 

### Proof of Concept

1. Canister C (e.g., Internet Identity) issues a `sender_info` signature: `sig = canister_sign("ic-sender-info" || b"role=admin")` for user U.
2. User U submits a valid request with this `sender_info`. The receiving canister grants admin access.
3. Canister C decides to revoke user U's admin role. It updates its internal state but cannot invalidate the already-issued `sig`.
4. User U constructs a new `HttpCanisterUpdate` with a fresh `ingress_expiry` (within the 5-minute TTL window), reuses the same `sender_info.sig`, and signs the new message with their own key.
5. `validate_sender_info` in `rs/validator/src/ingress_validation.rs` at line 482 calls `verify_sender_info_canister_sig`, which passes — the canister signature is still cryptographically valid. The receiving canister sees `role=admin` and grants access, despite the revocation. [6](#0-5)

### Citations

**File:** rs/types/types/src/messages/http.rs (L111-115)
```rust
pub struct RawSignedSenderInfo {
    pub info: Blob,
    pub signer: Blob,
    pub sig: Blob,
}
```

**File:** rs/types/types/src/messages/http.rs (L329-334)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, Deserialize, Serialize)]
pub struct SignedSenderInfo {
    pub info: Vec<u8>,
    pub signer: CanisterId,
    pub sig: Vec<u8>,
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

**File:** rs/validator/src/ingress_validation.rs (L459-488)
```rust
fn validate_sender_info<C: HttpRequestContent, R: RootOfTrustProvider>(
    request: &HttpRequest<C>,
    ingress_signature_verifier: &dyn IngressSigVerifier,
    root_of_trust_provider: &R,
) -> Result<(), RequestValidationError>
where
    R::Error: std::error::Error,
{
    let Some(sender_info) = request.sender_info() else {
        return Ok(());
    };

    // Per the spec, the sender_info signature must verify using the
    // envelope-level sender_pubkey as a canister signature public key.
    let sender_pubkey = match request.authentication() {
        Authentication::Authenticated(sig) => &sig.signer_pubkey,
        Authentication::Anonymous => {
            return Err(InvalidSenderInfo(
                "sender_info requires an authenticated request with sender_pubkey".to_string(),
            ));
        }
    };

    verify_sender_info_canister_sig(
        sender_info,
        sender_pubkey,
        ingress_signature_verifier,
        root_of_trust_provider,
    )
}
```

**File:** rs/validator/src/ingress_validation.rs (L494-545)
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
}
```

**File:** rs/validator/src/ingress_validation.rs (L606-623)
```rust
fn validate_sender_delegation_expiry(
    sender_delegation: &Option<Vec<SignedDelegation>>,
    current_time: Time,
) -> Result<(), RequestValidationError> {
    if let Some(delegations) = &sender_delegation {
        for delegation in delegations.iter() {
            let expiry = delegation.delegation().expiration();
            if delegation.delegation().expiration() < current_time {
                return Err(InvalidDelegationExpiry(format!(
                    "Specified sender delegation has expired:\n\
                     Provided expiry:    {expiry}\n\
                     Local replica time: {current_time}",
                )));
            }
        }
    }
    Ok(())
}
```
