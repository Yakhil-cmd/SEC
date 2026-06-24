### Title
Canister Signature Verification Result Discarded — Ingress Validation Always Succeeds for `IcCanisterSignatureAlgPublicKeyDer` - (File: `rs/validator/src/ingress_validation.rs`)

---

### Summary

In `rs/validator/src/ingress_validation.rs`, both `validate_signature` and `validate_delegation` call `verify_canister_sig_with_fallback!` for the `IcCanisterSignatureAlgPublicKeyDer` key type but **discard the macro's return value** without propagating it. The functions then unconditionally return `Ok(targets)` / fall through to `Ok(...)`, meaning canister-signature verification always succeeds regardless of whether the signature is cryptographically valid. This is the direct IC analog of the reported "function returns only `true`" vulnerability class.

---

### Finding Description

In `validate_signature` (lines 679–692):

```rust
KeyBytesContentType::IcCanisterSignatureAlgPublicKeyDer => {
    let canister_sig = CanisterSigOf::from(CanisterSig(signature.signature.clone()));
    verify_canister_sig_with_fallback!(   // result is a bare statement — NOT propagated
        validator,
        &canister_sig,
        message_id,
        &pk,
        root_of_trust_provider,
        |e| InvalidSignature(InvalidCanisterSignature(e.to_string())),
        |e: <R as RootOfTrustProvider>::Error| InvalidSignature(InvalidCanisterSignature(
            e.to_string()
        ))
    );
    Ok(targets)   // ← always returned, even on verification failure
}
``` [1](#0-0) 

Contrast with the Ed25519 / ECDSA branch immediately above, where the result is correctly propagated with `?`:

```rust
validate_signature_plain(validator, message_id, &basic_sig, &pk)
    .map_err(InvalidSignature)?;   // ← error propagated
Ok(targets)
``` [2](#0-1) 

The same pattern appears in `validate_delegation` (lines 819–830): the macro is called as a bare statement, its `Result` is silently dropped, and the function falls through to `Ok(CanisterIdSet)`: [3](#0-2) 

---

### Impact Explanation

Any unprivileged ingress sender who constructs a message or delegation using the `IcCanisterSignatureAlgPublicKeyDer` key type can present a **completely invalid or fabricated canister signature** and have it accepted as valid by the replica's ingress validation layer. This allows:

1. **Ingress message forgery**: An attacker can send update calls or queries claiming to be signed by any canister-based identity (e.g., Internet Identity anchors) without possessing the corresponding canister secret.
2. **Delegation chain forgery**: An attacker can insert a forged canister-signed delegation into a delegation chain, allowing them to impersonate any principal that uses canister signatures as a delegation root.

The `validate_signature` function is the core authentication gate for all ingress messages. Bypassing it for canister signatures breaks the authentication guarantee of the IC interface spec for all canister-signature users.

---

### Likelihood Explanation

**High.** The attacker-controlled entry path is direct: submit any ingress message (update call, query, or read_state) with a sender public key of type `IcCanisterSignatureAlgPublicKeyDer` and an arbitrary (invalid) signature. No privileged access, no threshold corruption, and no social engineering is required. Internet Identity is the primary real-world user of canister signatures, making this a high-value target with a large affected user base.

---

### Recommendation

Propagate the result of `verify_canister_sig_with_fallback!` with the `?` operator in both call sites, matching the pattern used for all other key types:

```rust
// In validate_signature:
KeyBytesContentType::IcCanisterSignatureAlgPublicKeyDer => {
    let canister_sig = CanisterSigOf::from(CanisterSig(signature.signature.clone()));
    verify_canister_sig_with_fallback!(
        validator, &canister_sig, message_id, &pk, root_of_trust_provider,
        |e| InvalidSignature(InvalidCanisterSignature(e.to_string())),
        |e: <R as RootOfTrustProvider>::Error| InvalidSignature(InvalidCanisterSignature(e.to_string()))
    )?;   // ← add ?
    Ok(targets)
}

// In validate_delegation:
KeyBytesContentType::IcCanisterSignatureAlgPublicKeyDer => {
    let canister_sig = CanisterSigOf::from(CanisterSig(signature.to_vec()));
    verify_canister_sig_with_fallback!(
        validator, &canister_sig, delegation, &pk, root_of_trust_provider,
        |e| InvalidCanisterSignature(e.to_string()),
        |e: <R as RootOfTrustProvider>::Error| InvalidCanisterSignature(e.to_string())
    )?;   // ← add ?
}
``` [4](#0-3) [5](#0-4) 

---

### Proof of Concept

1. Construct an IC ingress update call targeting any canister.
2. Set `sender` to a self-authenticating principal derived from an `IcCanisterSignatureAlgPublicKeyDer`-encoded public key (any canister ID + any seed).
3. Set `sender_sig` to an arbitrary byte string (e.g., all zeros).
4. Submit the envelope to any replica boundary node.
5. The replica's `validate_signature` function reaches the `IcCanisterSignatureAlgPublicKeyDer` branch, calls `verify_canister_sig_with_fallback!`, discards the error result, and returns `Ok(targets)` — accepting the forged signature as valid.
6. The message is admitted into the ingress pool and executed as if it were legitimately signed by the claimed sender. [6](#0-5)

### Citations

**File:** rs/validator/src/ingress_validation.rs (L635-703)
```rust
fn validate_signature<R: RootOfTrustProvider>(
    validator: &dyn IngressSigVerifier,
    message_id: &MessageId,
    signature: &UserSignature,
    current_time: Time,
    root_of_trust_provider: &R,
) -> Result<CanisterIdSet, RequestValidationError>
where
    R::Error: std::error::Error,
{
    validate_sender_delegation_length(&signature.sender_delegation)?;
    validate_sender_delegation_expiry(&signature.sender_delegation, current_time)?;
    let empty_vec = Vec::new();
    let signed_delegations = signature.sender_delegation.as_ref().unwrap_or(&empty_vec);

    let (pubkey, targets) = validate_delegations(
        validator,
        signed_delegations.as_slice(),
        signature.signer_pubkey.clone(),
        root_of_trust_provider,
    )?;

    let (pk, pk_type) = public_key_from_bytes(&pubkey).map_err(InvalidSignature)?;

    match pk_type {
        KeyBytesContentType::EcdsaP256PublicKeyDerWrappedCose
        | KeyBytesContentType::Ed25519PublicKeyDerWrappedCose
        | KeyBytesContentType::RsaSha256PublicKeyDerWrappedCose => {
            let webauthn_sig = WebAuthnSignature::try_from(signature.signature.as_slice())
                .map_err(WebAuthnError)
                .map_err(InvalidSignature)?;
            validate_webauthn_sig(validator, &webauthn_sig, message_id, &pk)
                .map_err(WebAuthnError)
                .map_err(InvalidSignature)?;
            Ok(targets)
        }
        KeyBytesContentType::Ed25519PublicKeyDer
        | KeyBytesContentType::EcdsaP256PublicKeyDer
        | KeyBytesContentType::EcdsaSecp256k1PublicKeyDer => {
            let basic_sig = BasicSigOf::from(BasicSig(signature.signature.clone()));
            validate_signature_plain(validator, message_id, &basic_sig, &pk)
                .map_err(InvalidSignature)?;
            Ok(targets)
        }
        KeyBytesContentType::IcCanisterSignatureAlgPublicKeyDer => {
            let canister_sig = CanisterSigOf::from(CanisterSig(signature.signature.clone()));
            verify_canister_sig_with_fallback!(
                validator,
                &canister_sig,
                message_id,
                &pk,
                root_of_trust_provider,
                |e| InvalidSignature(InvalidCanisterSignature(e.to_string())),
                |e: <R as RootOfTrustProvider>::Error| InvalidSignature(InvalidCanisterSignature(
                    e.to_string()
                ))
            );
            Ok(targets)
        }
        KeyBytesContentType::RsaSha256PublicKeyDer => {
            Err(RequestValidationError::InvalidSignature(
                AuthenticationError::InvalidBasicSignature(CryptoError::AlgorithmNotSupported {
                    algorithm: AlgorithmId::RsaSha256,
                    reason: "RSA signatures are not allowed except in webauthn context".to_owned(),
                }),
            ))
        }
    }
}
```

**File:** rs/validator/src/ingress_validation.rs (L819-838)
```rust
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
```
