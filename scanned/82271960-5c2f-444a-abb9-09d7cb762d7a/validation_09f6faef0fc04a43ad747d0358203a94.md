The code is now fully visible. Let me verify the `public_key_from_bytes` / `user_public_key_from_bytes` behavior to confirm the `AlgorithmId` assigned to a bare RSA DER key.

The code is fully confirmed. The asymmetry is real and the attack path is concrete. Here is the complete analysis:

---

### Title
RSA Bare-Key Delegation Bypass of WebAuthn Requirement — (`rs/validator/src/ingress_validation.rs`)

### Summary

`validate_delegation` accepts `KeyBytesContentType::RsaSha256PublicKeyDer` in its basic-sig branch, while `validate_signature` explicitly rejects the same key type with "RSA signatures are not allowed except in webauthn context." An attacker who controls an RSA private key can exploit this asymmetry to sign a delegation from their bare RSA key to an arbitrary ephemeral key, then use the ephemeral key to sign the final ingress message, bypassing the WebAuthn requirement entirely.

### Finding Description

In `rs/validator/src/ingress_validation.rs`:

**`validate_delegation` (lines 810–817)** groups `RsaSha256PublicKeyDer` with Ed25519/ECDSA in the basic-sig branch:

```rust
KeyBytesContentType::Ed25519PublicKeyDer
| KeyBytesContentType::EcdsaP256PublicKeyDer
| KeyBytesContentType::EcdsaSecp256k1PublicKeyDer
| KeyBytesContentType::RsaSha256PublicKeyDer => {   // ← bare RSA accepted
    let basic_sig = BasicSigOf::from(BasicSig(signature.to_vec()));
    validator
        .verify_basic_sig_by_public_key(&basic_sig, delegation, &pk)
        .map_err(InvalidBasicSignature)?;
}
``` [1](#0-0) 

**`validate_signature` (lines 694–700)** explicitly rejects the same key type:

```rust
KeyBytesContentType::RsaSha256PublicKeyDer => {
    Err(RequestValidationError::InvalidSignature(
        AuthenticationError::InvalidBasicSignature(CryptoError::AlgorithmNotSupported {
            algorithm: AlgorithmId::RsaSha256,
            reason: "RSA signatures are not allowed except in webauthn context".to_owned(),
        }),
    ))
}
``` [2](#0-1) 

`user_public_key_from_bytes` in `rs/crypto/standalone-sig-verifier/src/sign_utils.rs` confirms that a bare RSA SPKI DER key is parsed as `(AlgorithmId::RsaSha256, KeyBytesContentType::RsaSha256PublicKeyDer)`: [3](#0-2) 

And `verify_basic_sig_by_public_key` in `rs/crypto/standalone-sig-verifier/src/lib.rs` fully supports `AlgorithmId::RsaSha256` via PKCS#1 v1.5 SHA-256: [4](#0-3) 

### Impact Explanation

The full call chain `validate_request_content` → `validate_user_id_and_signature` → `validate_signature` → `validate_delegations` → `validate_delegation` is reachable from any ingress message submission. [5](#0-4) 

An attacker who controls an RSA private key can:
1. Derive their principal as `PrincipalId::new_self_authenticating(rsa_bare_der)` — a valid self-authenticating principal. [6](#0-5) 
2. Sign a `Delegation { pubkey: ed25519_ephemeral_der, expiry: future }` with their RSA private key using plain PKCS#1 v1.5 SHA-256 (no WebAuthn wrapper).
3. Sign the `MessageId` with the ephemeral Ed25519 key.
4. Submit an ingress with `sender=RSA_principal`, `signer_pubkey=RSA_bare_DER`, `sender_delegation=[RSA→Ed25519]`, `signature=Ed25519_sig`.

Validation succeeds because:
- `validate_delegation` accepts the RSA basic sig on the delegation ✓
- After the delegation chain, `pubkey` is now the Ed25519 ephemeral key
- `validate_signature` sees `Ed25519PublicKeyDer` for the final message and accepts it ✓

The invariant "RSA signatures are only accepted in WebAuthn context" is violated for delegation signing. Any RSA principal can submit ingress messages without a WebAuthn authenticator, bypassing the user-presence/consent guarantee that the WebAuthn requirement is designed to enforce.

### Likelihood Explanation

The exploit requires only: (1) generating an RSA key pair, (2) computing a PKCS#1 v1.5 SHA-256 signature over the delegation bytes, (3) constructing a valid CBOR-encoded ingress envelope. All of these are standard cryptographic operations available in any RSA library. No privileged access, no threshold corruption, no social engineering is required. The attacker is unprivileged and enters through the standard ingress path.

### Recommendation

Add `KeyBytesContentType::RsaSha256PublicKeyDer` to the rejection arm of `validate_delegation`, mirroring the treatment in `validate_signature`:

```rust
KeyBytesContentType::RsaSha256PublicKeyDer => {
    return Err(InvalidBasicSignature(CryptoError::AlgorithmNotSupported {
        algorithm: AlgorithmId::RsaSha256,
        reason: "RSA signatures are not allowed except in webauthn context".to_owned(),
    }));
}
```

This ensures the WebAuthn requirement for RSA is enforced uniformly across both delegation signing and final message signing. [7](#0-6) 

### Proof of Concept

```
1. Generate RSA-2048 key pair (rsa_priv, rsa_pub_spki_der).
2. rsa_principal = PrincipalId::new_self_authenticating(&rsa_pub_spki_der)
3. Generate Ed25519 key pair (ed_priv, ed_pub_der).
4. delegation = Delegation { pubkey: ed_pub_der, expiry: now + 1h }
5. delegation_sig = RSA_PKCS1_SHA256_sign(rsa_priv, domain_sep("ic-request-auth-delegation") || CBOR(delegation))
6. signed_delegation = SignedDelegation { delegation, signature: delegation_sig }
7. message_id = hash of ingress content
8. message_sig = Ed25519_sign(ed_priv, domain_sep("ic-request") || message_id)
9. Submit ingress:
     sender          = rsa_principal
     signer_pubkey   = rsa_pub_spki_der
     sender_delegation = [signed_delegation]
     sender_sig      = message_sig
10. Assert validate_request returns Ok(CanisterIdSet::all()).
```

The delegation passes `validate_delegation` (RSA basic-sig branch), the final message passes `validate_signature` (Ed25519 basic-sig branch), and the overall request is accepted — without any WebAuthn authenticator involved.

### Citations

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

**File:** rs/validator/src/ingress_validation.rs (L626-632)
```rust
fn validate_user_id(sender_pubkey: &[u8], id: &UserId) -> Result<(), RequestValidationError> {
    if id.get_ref() == &PrincipalId::new_self_authenticating(sender_pubkey) {
        Ok(())
    } else {
        Err(UserIdDoesNotMatchPublicKey(*id, sender_pubkey.to_vec()))
    }
}
```

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

**File:** rs/validator/src/ingress_validation.rs (L790-839)
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

    // Validation succeeded. Return the targets of this delegation.
    Ok(match delegation.targets().map_err(DelegationTargetError)? {
        None => CanisterIdSet::all(),
        Some(targets) => CanisterIdSet::try_from_iter(targets)
            .map_err(|e| DelegationTargetError(format!("{e}")))?,
    })
}
```

**File:** rs/crypto/standalone-sig-verifier/src/sign_utils.rs (L104-109)
```rust
    } else if pkix_algo_id == rsa_algorithm_identifier() {
        (
            rsa::RsaPublicKey::from_der_spki(bytes)?.as_der().to_vec(),
            AlgorithmId::RsaSha256,
            KeyBytesContentType::RsaSha256PublicKeyDer,
        )
```

**File:** rs/crypto/standalone-sig-verifier/src/lib.rs (L101-108)
```rust
        AlgorithmId::RsaSha256 => {
            use ic_crypto_internal_basic_sig_rsa_pkcs1 as rsa;

            let public_key = rsa::RsaPublicKey::from_der_spki(&public_key_bytes)?;

            // RSA hashes the message using SHA-256
            public_key.verify_pkcs1_sha256(msg, &signature_bytes)
        }
```
