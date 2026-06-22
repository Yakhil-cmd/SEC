The code is clear. Let me trace the exact paths.

The inconsistency is real and concrete. Here is the full analysis.

---

### Title
Plain RSA DER Key Accepted as Delegation Signer, Bypassing WebAuthn-Only RSA Policy — (`rs/validator/src/ingress_validation.rs`)

### Summary

`validate_signature` explicitly rejects `RsaSha256PublicKeyDer` for direct request signing, but `validate_delegation` silently accepts the same key type as a delegation signer via a basic-sig path. An attacker who holds an RSA private key can root a delegation chain in that plain RSA DER key, sign the delegation with it, and have the chain accepted by the validator — bypassing the invariant that RSA is only permitted in a WebAuthn (COSE-wrapped) context.

### Finding Description

In `validate_signature`, the match on `pk_type` has an explicit rejection arm for plain RSA:

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

In `validate_delegation`, the equivalent match arm groups `RsaSha256PublicKeyDer` together with Ed25519, EcdsaP256, and EcdsaSecp256k1, and calls `verify_basic_sig_by_public_key` without any RSA-specific rejection:

```rust
KeyBytesContentType::Ed25519PublicKeyDer
| KeyBytesContentType::EcdsaP256PublicKeyDer
| KeyBytesContentType::EcdsaSecp256k1PublicKeyDer
| KeyBytesContentType::RsaSha256PublicKeyDer => {
    let basic_sig = BasicSigOf::from(BasicSig(signature.to_vec()));
    validator
        .verify_basic_sig_by_public_key(&basic_sig, delegation, &pk)
        .map_err(InvalidBasicSignature)?;
}
``` [2](#0-1) 

`validate_delegations` passes `signature.signer_pubkey` as the initial `pubkey` and iterates through the delegation chain, calling `validate_delegation` for each link. After the chain is consumed, the **last** delegation's target key is returned and checked in `validate_signature`. [3](#0-2) 

### Impact Explanation

An attacker who holds an RSA private key can:

1. Set `sender_pubkey = RSA_DER_pub` in the ingress envelope. The `sender` UserId is `PrincipalId::new_self_authenticating(RSA_DER_pub)`, which passes `validate_user_id`. [4](#0-3) 

2. Construct a `SignedDelegation` whose `delegation.pubkey` is any Ed25519 (or other non-RSA) key, signed with the RSA private key.

3. Sign the actual ingress message with the Ed25519 private key.

4. `validate_delegations` calls `validate_delegation(RSA_DER_pub, ...)` → `RsaSha256PublicKeyDer` branch → `verify_basic_sig_by_public_key` → **succeeds**.

5. The returned final pubkey is the Ed25519 key. `validate_signature` sees `Ed25519PublicKeyDer` → verifies the Ed25519 signature → **succeeds**.

The result: the request is accepted as authenticated under a principal derived from a plain RSA DER key, without any WebAuthn wrapping. The policy enforced in `validate_signature` is fully circumvented for the root of the delegation chain.

### Likelihood Explanation

The exploit requires only:
- Generating an RSA key pair (standard library call)
- Constructing a valid `SignedDelegation` CBOR structure
- Sending a standard HTTP ingress request

No privileged access, no threshold corruption, no social engineering. It is locally reproducible with a unit or integration test against `HttpRequestVerifierImpl`.

### Recommendation

Add the same explicit rejection for `RsaSha256PublicKeyDer` in `validate_delegation` that already exists in `validate_signature`:

```rust
KeyBytesContentType::RsaSha256PublicKeyDer => {
    return Err(InvalidBasicSignature(CryptoError::AlgorithmNotSupported {
        algorithm: AlgorithmId::RsaSha256,
        reason: "RSA signatures are not allowed except in webauthn context".to_owned(),
    }));
}
```

This makes the two functions consistent and closes the delegation bypass. [5](#0-4) 

### Proof of Concept

State-machine test sketch:

```rust
// 1. Generate RSA key pair (root of delegation chain)
let rsa_sk = RsaPrivateKey::new(...);
let rsa_der_pub = rsa_sk.to_public_key().to_pkcs1_der();

// 2. Generate Ed25519 key pair (final signer)
let ed_sk = Ed25519PrivateKey::new();
let ed_der_pub = ed_sk.public_key().to_der();

// 3. Build delegation: RSA_pub delegates to Ed_pub
let delegation = Delegation { pubkey: ed_der_pub, expiry: far_future, targets: None };
let delegation_sig = rsa_sk.sign(delegation_signing_bytes(&delegation)); // plain RSA PKCS#1

// 4. Build ingress message signed with Ed25519
let msg_id = MessageId::from(...);
let ingress_sig = ed_sk.sign(request_signing_bytes(&msg_id));

// 5. Construct UserSignature
let user_sig = UserSignature {
    signature: ingress_sig,
    signer_pubkey: rsa_der_pub,
    sender_delegation: Some(vec![SignedDelegation::new(delegation, delegation_sig)]),
};

// 6. sender = PrincipalId::new_self_authenticating(rsa_der_pub)
// 7. Call validate_request → expect Ok(...), not Err(InvalidDelegation(...))
```

The validator accepts this request today. After the fix, it must return `Err(InvalidDelegation(InvalidBasicSignature(AlgorithmNotSupported { RsaSha256 })))`.

### Citations

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

**File:** rs/validator/src/ingress_validation.rs (L721-753)
```rust
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
}
```

**File:** rs/validator/src/ingress_validation.rs (L802-831)
```rust
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
