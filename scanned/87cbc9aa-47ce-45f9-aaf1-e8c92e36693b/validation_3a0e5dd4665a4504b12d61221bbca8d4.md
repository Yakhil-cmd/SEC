The code confirms the asymmetry is real. Here is the analysis:

**`validate_signature`** (line 694–700): `RsaSha256PublicKeyDer` → hard error: [1](#0-0) 

**`validate_delegation`** (line 810–818): `RsaSha256PublicKeyDer` is grouped with Ed25519/ECDSA and routed to `verify_basic_sig_by_public_key` — no WebAuthn envelope required: [2](#0-1) 

The call chain in `validate_signature` is: call `validate_delegations` first (line 650–655), then check the *final* key type (line 657–701). The initial `signer_pubkey` type is never checked directly — only the key at the end of the delegation chain is checked. [3](#0-2) 

---

### Title
Raw RSA DER Key Bypasses WebAuthn Envelope Requirement via Delegation Signing — (`rs/validator/src/ingress_validation.rs`)

### Summary
`validate_delegation` permits `RsaSha256PublicKeyDer` to sign a delegation with a bare PKCS#1 signature (no WebAuthn envelope), while `validate_signature` explicitly rejects the same key type for direct signing. An attacker holding a raw RSA key pair can exploit this asymmetry to authenticate as the corresponding self-authenticating principal without ever producing a WebAuthn envelope.

### Finding Description
In `validate_signature`, the match on the *final* public key type (after delegation chain resolution) rejects `RsaSha256PublicKeyDer` with "RSA signatures are not allowed except in webauthn context." [4](#0-3) 

However, `validate_delegations` is called *before* this check, and it iterates over each `SignedDelegation`, calling `validate_delegation` with the current `pubkey`. Inside `validate_delegation`, `RsaSha256PublicKeyDer` falls into the basic-sig branch alongside Ed25519/ECDSA: [5](#0-4) 

This means a raw RSA key can legitimately sign a delegation (advancing `pubkey` to the delegated key), and the final signing key can be any accepted type (e.g., Ed25519). The raw RSA key's role as the root signer is never re-examined after delegation resolution.

### Impact Explanation
An attacker who generates a raw RSA-2048+ key pair can:
1. Derive a self-authenticating principal from the raw DER-encoded public key.
2. Construct a `SignedDelegation` where the delegation is signed with a bare PKCS#1-SHA256 signature (no WebAuthn `clientDataJSON`/`authenticatorData`).
3. Delegate to any second key they control (e.g., Ed25519).
4. Sign the ingress message with the second key.

`validate_signature` accepts this because the final key is Ed25519, never seeing that the root signer was a raw RSA key used without WebAuthn. The invariant "RSA must only be used in the WebAuthn context" is violated. Any canister calls authorized under that principal are accepted.

### Likelihood Explanation
The attack requires only standard cryptographic operations (RSA key generation, PKCS#1 signing) and is fully constructible offline. No privileged access, governance majority, or network-level attack is needed. The principal targeted is one the attacker derives themselves, so no credential theft is required. The path is concrete and locally testable.

### Recommendation
In `validate_delegation`, remove `KeyBytesContentType::RsaSha256PublicKeyDer` from the basic-sig match arm: [2](#0-1) 

Either reject it outright (mirroring `validate_signature`) or route it to the COSE/WebAuthn branch. The policy should be symmetric: if raw RSA DER is forbidden for direct signing, it must also be forbidden for signing delegations.

### Proof of Concept
Construct a `UserSignature` where:
- `signer_pubkey` = raw RSA-2048 DER public key
- `sender_delegation` = one `SignedDelegation` whose `delegation.pubkey` is an Ed25519 key, and whose `signature` is a bare PKCS#1-SHA256 signature over the delegation bytes, produced by the RSA private key
- `signature` = Ed25519 signature over the `MessageId`, produced by the Ed25519 private key

Assert that `validate_signature` returns `Ok(...)`. The test will pass against the current code, demonstrating that the WebAuthn envelope requirement for the root RSA key is bypassed.

### Citations

**File:** rs/validator/src/ingress_validation.rs (L650-657)
```rust
    let (pubkey, targets) = validate_delegations(
        validator,
        signed_delegations.as_slice(),
        signature.signer_pubkey.clone(),
        root_of_trust_provider,
    )?;

    let (pk, pk_type) = public_key_from_bytes(&pubkey).map_err(InvalidSignature)?;
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

**File:** rs/validator/src/ingress_validation.rs (L810-818)
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
```
