### Title
RSA Signature Algorithm Restriction Bypassed via Intermediate Delegation Signer - (File: rs/validator/src/ingress_validation.rs)

### Summary
`validate_signature` explicitly rejects bare `RsaSha256PublicKeyDer` keys with the message "RSA signatures are not allowed except in webauthn context," but `validate_delegation` silently accepts the same key type in the basic-sig branch. An unprivileged ingress sender can therefore insert an RSA key as an intermediate link in a delegation chain, bypassing the RSA restriction entirely.

### Finding Description
In `rs/validator/src/ingress_validation.rs`, the two functions that enforce signature-algorithm policy are inconsistent:

**`validate_signature`** (the final-signer check) explicitly rejects bare RSA:

```rust
KeyBytesContentType::RsaSha256PublicKeyDer => {
    Err(RequestValidationError::InvalidSignature(
        AuthenticationError::InvalidBasicSignature(CryptoError::AlgorithmNotSupported {
            algorithm: AlgorithmId::RsaSha256,
            reason: "RSA signatures are not allowed except in webauthn context".to_owned(),
        }),
    ))
}
``` [1](#0-0) 

**`validate_delegation`** (the per-hop delegation check) groups `RsaSha256PublicKeyDer` together with the permitted algorithms and calls `verify_basic_sig_by_public_key` on it without any rejection:

```rust
KeyBytesContentType::Ed25519PublicKeyDer
| KeyBytesContentType::EcdsaP256PublicKeyDer
| KeyBytesContentType::EcdsaSecp256k1PublicKeyDer
| KeyBytesContentType::RsaSha256PublicKeyDer => {   // ← RSA accepted here
    let basic_sig = BasicSigOf::from(BasicSig(signature.to_vec()));
    validator
        .verify_basic_sig_by_public_key(&basic_sig, delegation, &pk)
        .map_err(InvalidBasicSignature)?;
}
``` [2](#0-1) 

`validate_delegations` iterates the chain and updates `pubkey` to `delegation.pubkey()` after each hop: [3](#0-2) 

Because the final `pubkey` returned to `validate_signature` is the key from the **last** delegation (not the intermediate RSA key), the RSA arm in `validate_signature` is never reached when RSA is used as an intermediate signer.

### Impact Explanation
The IC interface spec and the code comment both state that bare RSA is forbidden outside of WebAuthn. This policy is enforced only at the terminal position of the delegation chain. An attacker who constructs a chain of the form:

```
Ed25519_outer → [RSA intermediate signs delegation] → Ed25519_final → message
```

successfully uses an RSA key to authorize a delegation step. This:
1. Violates the stated algorithm-restriction policy for ingress authentication.
2. Exposes the delegation-chain verification to any RSA-specific weakness (e.g., PKCS#1 v1.5 signature malleability, crafted-modulus attacks) that the IC's RSA implementation may not fully mitigate, since the RSA key and its signature are fully attacker-controlled.

The `verify_basic_sig_by_public_key` path for RSA calls `RsaPublicKey::verify_pkcs1_sha256`, which is a standard PKCS#1 v1.5 check: [4](#0-3) 

### Likelihood Explanation
Any unprivileged user who can submit an ingress message can trigger this path. Constructing a delegation chain with an RSA intermediate key requires only:
- Generating an RSA key pair (no privileged access).
- Encoding the public key as a bare DER `SubjectPublicKeyInfo` (not COSE-wrapped).
- Signing the second delegation with the RSA private key.

No admin key, governance majority, or threshold corruption is required. The entry point is the standard HTTP ingress interface available to all callers.

### Recommendation
Add an explicit rejection of `RsaSha256PublicKeyDer` inside `validate_delegation`, mirroring the rejection already present in `validate_signature`:

```rust
KeyBytesContentType::RsaSha256PublicKeyDer => {
    return Err(InvalidBasicSignature(CryptoError::AlgorithmNotSupported {
        algorithm: AlgorithmId::RsaSha256,
        reason: "RSA signatures are not allowed except in webauthn context".to_owned(),
    }));
}
```

This ensures the RSA restriction is enforced uniformly at every position in the delegation chain, not only at the terminal signer. [5](#0-4) 

### Proof of Concept
1. Generate an RSA-2048 key pair `(rsk, rpk)` and an Ed25519 key pair `(esk2, epk2)`.
2. Use an existing Ed25519 key pair `(esk1, epk1)` as `signer_pubkey` (the outer key).
3. Build `delegation1`: `pubkey = rpk` (bare DER), signed by `esk1`.
4. Build `delegation2`: `pubkey = epk2`, signed by `rsk` (RSA PKCS#1 v1.5 over the delegation bytes).
5. Sign the `MessageId` with `esk2`.
6. Submit the ingress message with `sender_delegation = [delegation1, delegation2]`.

`validate_delegations` will:
- Call `validate_delegation(sig=esk1_sig, delegation=delegation1, pubkey=epk1)` → accepted (Ed25519).
- Update `pubkey = rpk`.
- Call `validate_delegation(sig=rsk_sig, delegation=delegation2, pubkey=rpk)` → **accepted** (RSA, line 813).
- Update `pubkey = epk2`.

`validate_signature` then verifies the message with `epk2` (Ed25519) → accepted. The RSA restriction is never triggered. [6](#0-5)

### Citations

**File:** rs/validator/src/ingress_validation.rs (L694-701)
```rust
        KeyBytesContentType::RsaSha256PublicKeyDer => {
            Err(RequestValidationError::InvalidSignature(
                AuthenticationError::InvalidBasicSignature(CryptoError::AlgorithmNotSupported {
                    algorithm: AlgorithmId::RsaSha256,
                    reason: "RSA signatures are not allowed except in webauthn context".to_owned(),
                }),
            ))
        }
```

**File:** rs/validator/src/ingress_validation.rs (L716-752)
```rust
// Validate a chain of delegations.
// See https://internetcomputer.org/docs/current/references/ic-interface-spec#authentication
//
// If the delegations are valid, returns the public key used to sign the
// request as well as the set of canister IDs that the public key is valid for.
fn validate_delegations<R: RootOfTrustProvider>(
    validator: &dyn IngressSigVerifier,
    signed_delegations: &[SignedDelegation],
    mut pubkey: Vec<u8>,
    root_of_trust_provider: &R,
) -> Result<(Vec<u8>, CanisterIdSet), RequestValidationError>
where
    R::Error: std::error::Error,
{
    ensure_delegations_does_not_contain_cycles(&pubkey, signed_delegations)?;
    ensure_delegations_does_not_contain_too_many_targets(signed_delegations)?;
    // Initially, assume that the delegations target all possible canister IDs.
    let mut targets = CanisterIdSet::all();

    for sd in signed_delegations {
        let delegation = sd.delegation();
        let signature = sd.signature();

        let new_targets = validate_delegation(
            validator,
            signature,
            delegation,
            &pubkey,
            root_of_trust_provider,
        )
        .map_err(InvalidDelegation)?;
        // Restrict the canister targets to the ones specified in the delegation.
        targets = targets.intersect(new_targets);
        pubkey = delegation.pubkey().to_vec();
    }

    Ok((pubkey, targets))
```

**File:** rs/validator/src/ingress_validation.rs (L790-831)
```rust
fn validate_delegation<R: RootOfTrustProvider>(
    validator: &dyn IngressSigVerifier,
    signature: &[u8],
    delegation: &Delegation,
    pubkey: &[u8],
    root_of_trust_provider: &R,
) -> Result<CanisterIdSet, AuthenticationError>
where
    R::Error: std::error::Error,
{
    let (pk, pk_type) = public_key_from_bytes(pubkey)?;

    match pk_type {
        KeyBytesContentType::EcdsaP256PublicKeyDerWrappedCose
        | KeyBytesContentType::Ed25519PublicKeyDerWrappedCose
        | KeyBytesContentType::RsaSha256PublicKeyDerWrappedCose => {
            let webauthn_sig = WebAuthnSignature::try_from(signature).map_err(WebAuthnError)?;
            validate_webauthn_sig(validator, &webauthn_sig, delegation, &pk)
                .map_err(WebAuthnError)?;
        }
        KeyBytesContentType::Ed25519PublicKeyDer
        | KeyBytesContentType::EcdsaP256PublicKeyDer
        | KeyBytesContentType::EcdsaSecp256k1PublicKeyDer
        | KeyBytesContentType::RsaSha256PublicKeyDer => {
            let basic_sig = BasicSigOf::from(BasicSig(signature.to_vec()));
            validator
                .verify_basic_sig_by_public_key(&basic_sig, delegation, &pk)
                .map_err(InvalidBasicSignature)?;
        }
        KeyBytesContentType::IcCanisterSignatureAlgPublicKeyDer => {
            let canister_sig = CanisterSigOf::from(CanisterSig(signature.to_vec()));
            verify_canister_sig_with_fallback!(
                validator,
                &canister_sig,
                delegation,
                &pk,
                root_of_trust_provider,
                |e| InvalidCanisterSignature(e.to_string()),
                |e: <R as RootOfTrustProvider>::Error| InvalidCanisterSignature(e.to_string())
            );
        }
    }
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L152-167)
```rust
    pub fn verify_pkcs1_sha256(&self, message: &[u8], signature: &[u8]) -> CryptoResult<()> {
        let digest = Sha256::hash(message);

        match &self
            .key
            .verify(Pkcs1v15Sign::new::<sha2::Sha256>(), &digest, signature)
        {
            Ok(_) => Ok(()),
            Err(e) => Err(CryptoError::SignatureVerification {
                algorithm: AlgorithmId::RsaSha256,
                public_key_bytes: self.as_der().to_vec(),
                sig_bytes: signature.to_vec(),
                internal_error: format!("{e:?}"),
            }),
        }
    }
```
