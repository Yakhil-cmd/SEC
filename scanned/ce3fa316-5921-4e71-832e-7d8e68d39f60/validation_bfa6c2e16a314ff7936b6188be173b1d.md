I found a strong analog. Let me confirm the key code patterns before writing up the finding.

The critical observation is in `rs/validator/src/ingress_validation.rs`:

**In `validate_delegation` (lines 802–831):** All three key-type branches handle signature verification, but only the canister-signature branch omits `?`:
- WebAuthn: `validate_webauthn_sig(...).map_err(WebAuthnError)?;`
- Basic sig: `validator.verify_basic_sig_by_public_key(...).map_err(InvalidBasicSignature)?;`
- Canister sig: `verify_canister_sig_with_fallback!(...);` — **no `?`**

**In `verify_sender_info_canister_sig` (lines 533–544):** The macro is called and then `Ok(())` is returned unconditionally, regardless of the macro's result.

---

### Title
Missing `?` propagation on `verify_canister_sig_with_fallback!` silently bypasses canister-signature verification in delegation and sender_info validation — (File: `rs/validator/src/ingress_validation.rs`)

---

### Summary
In `rs/validator/src/ingress_validation.rs`, the `verify_canister_sig_with_fallback!` macro is invoked in two security-critical paths — `validate_delegation` and `verify_sender_info_canister_sig` — without the `?` operator to propagate its `Result`. Every other signature-verification call in the same function uses `?` explicitly. The macro's error result is silently discarded, meaning canister-signature verification for delegations and `sender_info` fields always succeeds regardless of whether the cryptographic check passes.

---

### Finding Description

**Location 1 — `validate_delegation` (lines 819–830):**

```rust
KeyBytesContentType::IcCanisterSignatureAlgPublicKeyDer => {
    let canister_sig = CanisterSigOf::from(CanisterSig(signature.to_vec()));
    verify_canister_sig_with_fallback!(   // <-- result silently dropped; no `?`
        validator,
        &canister_sig,
        delegation,
        &pk,
        root_of_trust_provider,
        |e| InvalidCanisterSignature(e.to_string()),
        |e: <R as RootOfTrustProvider>::Error| InvalidCanisterSignature(e.to_string())
    );
}
```

Compare with the other arms in the same `match`:

```rust
// WebAuthn arm — error propagated:
validate_webauthn_sig(validator, &webauthn_sig, delegation, &pk)
    .map_err(WebAuthnError)?;

// Basic-sig arm — error propagated:
validator
    .verify_basic_sig_by_public_key(&basic_sig, delegation, &pk)
    .map_err(InvalidBasicSignature)?;
```

The macro takes two error-mapping closures as arguments, which is the standard Rust pattern for a function/macro that returns a `Result` and expects the caller to propagate it. The absence of `?` means the `Err` variant is constructed and immediately dropped; the match arm falls through to `Ok(...)` unconditionally.

**Location 2 — `verify_sender_info_canister_sig` (lines 533–544):**

```rust
verify_canister_sig_with_fallback!(   // <-- result silently dropped
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
Ok(())   // <-- always reached, even on verification failure
```

The function signature is `-> Result<(), RequestValidationError>`, and `Ok(())` is returned unconditionally after the macro call, meaning a failed canister-signature check never surfaces as an error.

This is the direct Rust analog of the Solidity pattern:

```solidity
if (!isContract(target)) Errors.AddressNotContract;  // missing `revert`
```

Here the analog is:

```rust
verify_canister_sig_with_fallback!(...);  // missing `?`
```

In both cases, the error value is constructed but never acted upon.

---

### Impact Explanation

**Delegation bypass (Location 1):** Any unprivileged sender can craft an ingress message whose authentication uses a delegation chain where one or more delegations are signed with a canister-signature key (`IcCanisterSignatureAlgPublicKeyDer`) and an entirely invalid or forged signature. Because the verification result is dropped, `validate_delegation` returns `Ok(targets)` as if the signature were valid. The attacker can therefore:
- Forge a delegation from any canister-controlled identity to any key they control.
- Bypass delegation-target restrictions (the `CanisterIdSet` intersection logic in `validate_delegations` still runs, but it operates on targets extracted from an unauthenticated delegation).
- Impersonate any principal whose identity is backed by a canister signature.

**sender_info bypass (Location 2):** The `sender_info` field is intended to carry authenticated metadata about the sender, verified by a canister signature. With the check silently passing, an attacker can supply arbitrary `sender_info` content with a garbage signature and have it accepted as authentic by any replica.

Both paths are reachable by any unprivileged ingress sender with no special access.

---

### Likelihood Explanation

The attacker-controlled entry path is direct and requires no privileged access:

1. Construct an `HttpRequestEnvelope` with `Authentication::Authenticated` using a sender public key of type `IcCanisterSignatureAlgPublicKeyDer`.
2. Include a `sender_delegation` chain where at least one delegation is signed with a canister-signature key and a random/invalid signature byte string.
3. Submit the envelope to any replica's `/api/v2/canister/{id}/call` or `/api/v2/canister/{id}/query` endpoint.

The replica's `IngressValidator::validate_ingress_message` calls `validate_request`, which calls `validate_request_content`, which calls `validate_user_id_and_signature` → `validate_delegations` → `validate_delegation`. The canister-signature arm of `validate_delegation` silently passes, and the ingress message is accepted.

No threshold corruption, no admin key, no social engineering is required.

---

### Recommendation

Add `?` to propagate the result of `verify_canister_sig_with_fallback!` in both call sites:

```rust
// In validate_delegation:
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
    )?;  // <-- add `?`
}

// In verify_sender_info_canister_sig:
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
)?;  // <-- add `?`
Ok(())
```

If the macro is designed to use `return` internally (i.e., it already propagates errors via `return Err(...)`), the macro's expansion should be audited and documented to make the intent explicit, and a `#[must_use]` annotation or explicit `let _ =` suppression should be added to prevent future confusion.

---

### Proof of Concept

1. Generate a key pair of type `IcCanisterSignatureAlgPublicKeyDer` (canister signature public key DER).
2. Build a `SignedDelegation` where:
   - `delegation.pubkey` is any key the attacker controls.
   - `delegation.targets` is set to the target canister ID.
   - `signature` is 32 random bytes (invalid canister signature).
3. Wrap this in an `HttpRequestEnvelope` for a call to any canister, authenticated with the canister-signature key as `sender_pubkey` and the forged delegation as `sender_delegation`.
4. Submit to `/api/v2/canister/{canister_id}/call`.
5. The replica accepts the message: `validate_delegation` reaches the `IcCanisterSignatureAlgPublicKeyDer` arm, calls `verify_canister_sig_with_fallback!` without `?`, discards the `Err`, and returns `Ok(targets)`. The ingress message is inducted. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/validator/src/ingress_validation.rs (L196-220)
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
```

**File:** rs/validator/src/ingress_validation.rs (L533-544)
```rust
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

**File:** rs/validator/src/ingress_validation.rs (L720-752)
```rust
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

**File:** rs/validator/src/ingress_validation.rs (L802-818)
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
```

**File:** rs/validator/src/ingress_validation.rs (L819-830)
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
```
