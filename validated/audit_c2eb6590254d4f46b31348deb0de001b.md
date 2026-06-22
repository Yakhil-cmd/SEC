The exploit chain is real and reachable. Here is the full analysis.

---

### Title
Ed25519 Small-Order Public Key Accepted in COSE WebAuthn Path Allows Signature Forgery — (`rs/crypto/internal/crypto_lib/basic_sig/cose/src/lib.rs`)

### Summary

`parse_eddsa_ed25519` calls `ic_ed25519::PublicKey::deserialize_raw` which, by documented design, accepts small-order (torsion) points without error. The subsequent `verify_signature` implementation uses ZIP215 cofactor-8 verification, which reduces to a message-independent equation when the public key is a small-order point. An unprivileged attacker can therefore forge a valid WebAuthn ingress signature for any self-authenticating principal derived from a small-order key.

### Finding Description

**Step 1 — COSE parsing accepts small-order keys without rejection.**

`parse_eddsa_ed25519` calls `ic_ed25519::PublicKey::deserialize_raw(x)` and maps any error to `MalformedPublicKey`. The function's own documentation explicitly warns it does not check torsion: [1](#0-0) 

The library tests confirm that all 18 known small-order/torsion points are accepted by `deserialize_raw` (they return `Ok` but `is_torsion_free()` returns `false`): [2](#0-1) 

`parse_eddsa_ed25519` adds no `is_torsion_free()` guard after the call: [3](#0-2) 

**Step 2 — The DER is passed through `user_public_key_from_bytes` without a torsion check.**

When the COSE OID is detected, `user_public_key_from_bytes` calls `parse_cose_public_key` to get the DER, then recursively calls itself on that DER. The recursive call hits the `ed25519_algorithm_identifier()` branch, which calls `ic_ed25519::PublicKey::deserialize_rfc8410_der` — also documented as not checking torsion — and stores the raw small-order bytes in `UserPublicKey.key`: [4](#0-3) 

**Step 3 — `verify_basic_sig_by_public_key` also skips the torsion check.** [5](#0-4) 

**Step 4 — ZIP215 `verify_signature` with a small-order key accepts message-independent forgeries.**

The verification equation is:

```
(recomputed_r − R) · 8 == identity
where recomputed_r = k·(−A) + s·B
``` [6](#0-5) 

If `A` is a small-order point (order divides 8), then `k·(−A)·8 = identity` for **any** scalar `k`. The check collapses to:

```
(s·B − R) · 8 == identity
```

This is **independent of the message** (and therefore of `k = H(R ∥ A ∥ M)`). The attacker satisfies it by choosing `s = 0` and `R` = any small-order point, since `0·B − R = −R` and `(−R)·8 = identity`.

**Step 5 — The ingress validation path routes COSE-wrapped Ed25519 to the WebAuthn verifier.** [7](#0-6) 

`validate_webauthn_sig` checks the challenge in the WebAuthn envelope matches the message ID. The attacker constructs the envelope themselves, so they set the challenge to the correct message ID: [8](#0-7) 

**Step 6 — The sender principal check is also satisfied.**

`validate_user_id` checks that the sender equals `PrincipalId::new_self_authenticating(sender_pubkey)`. The attacker sets the `sender` field to the self-authenticating principal derived from their chosen small-order key. This check passes trivially. [9](#0-8) 

### Impact Explanation

An attacker can forge ingress signatures for any self-authenticating principal whose public key is one of the 8 small-order Ed25519 points. They can:
- Call any canister as that principal
- Drain any ICP/token balance held by that principal
- Exercise any canister permission granted to that principal

The impact is scoped to principals derived from small-order keys. The attacker cannot impersonate arbitrary existing users. However, they can create such a principal (e.g., by tricking a user into sending funds to it), then forge signatures to act as it.

### Likelihood Explanation

The attack requires no privileged access, no key material, and no network-level attack. It requires only:
1. Knowledge of one of the 18 small-order Ed25519 point encodings (publicly documented)
2. Ability to submit a WebAuthn ingress (standard user capability)
3. Construction of a 64-byte forged signature `(R_small_order ∥ 0x00…00)`

This is fully local-testable and requires no interaction with any honest party.

### Recommendation

Add an `is_torsion_free()` check in `parse_eddsa_ed25519` immediately after `deserialize_raw` succeeds:

```rust
if !pk.is_torsion_free() {
    return Err(CosePublicKeyParseError::MalformedPublicKey(AlgorithmId::Ed25519));
}
``` [10](#0-9) 

Analogously, add the same guard in `verify_basic_sig_by_public_key` for the `AlgorithmId::Ed25519` branch in `rs/crypto/standalone-sig-verifier/src/lib.rs`. [5](#0-4) 

Note: `verify_public_key` in `rs/crypto/internal/crypto_lib/basic_sig/ed25519/src/api.rs` already correctly calls `is_torsion_free()`, but this function is not called on the COSE ingress path. [11](#0-10) 

### Proof of Concept

```rust
// Take any of the 18 known small-order points, e.g.:
let small_order_pk: [u8; 32] =
    hex!("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa");

// Encode as COSE: {1:1, 3:-8, -1:6, -2:<pk>}
// (CBOR map with kty=OKP, alg=EdDSA, crv=Ed25519, x=small_order_pk)
let cose_key: Vec<u8> = build_cose_ed25519_key(&small_order_pk);

// DER-wrap with IC COSE OID 1.3.6.1.4.1.56387.1.1
let der_wrapped = der_wrap_cose(&cose_key);

// Derive the self-authenticating principal
let principal = PrincipalId::new_self_authenticating(&der_wrapped);

// Forged signature: R = small_order_pk, s = 0x00..00
let forged_sig: [u8; 64] = {
    let mut s = [0u8; 64];
    s[..32].copy_from_slice(&small_order_pk);
    s
};

// Construct WebAuthn envelope with challenge = message_id
// Submit ingress with sender=principal, signature=webauthn_envelope(forged_sig)
// -> verify_signature returns Ok(()) for any message
```

The forgery succeeds because `(0·B − R)·8 = (−R)·8 = identity` when R is a small-order point.

### Citations

**File:** packages/ic-ed25519/src/lib.rs (L609-631)
```rust
    /// Deserialize a public key in raw format
    ///
    /// This is just the 32 byte encoding of the public point,
    /// cooresponding to Self::serialize_raw
    ///
    /// # Warning
    ///
    /// This does not verify that the key is within the prime order
    /// subgroup, or that the public key is canonical. To check these
    /// properties, use is_torsion_free and is_canonical
    pub fn deserialize_raw(bytes: &[u8]) -> Result<Self, PublicKeyDecodingError> {
        let bytes = <[u8; Self::BYTES]>::try_from(bytes).map_err(|_| {
            PublicKeyDecodingError::InvalidKeyEncoding(format!(
                "Expected key of exactly {} bytes, got {}",
                Self::BYTES,
                bytes.len()
            ))
        })?;
        let pk = VerifyingKey::from_bytes(&bytes)
            .map_err(|e| PublicKeyDecodingError::InvalidKeyEncoding(format!("{e:?}")))?;

        Ok(Self::new(pk))
    }
```

**File:** packages/ic-ed25519/src/lib.rs (L709-727)
```rust
    pub fn verify_signature(&self, msg: &[u8], signature: &[u8]) -> Result<(), SignatureError> {
        let signature = Signature::from_slice(signature)?;

        let k = Self::compute_challenge(&signature, self, msg);
        let minus_a = -self.pk.to_edwards();
        let recomputed_r =
            EdwardsPoint::vartime_double_scalar_mul_basepoint(&k, &minus_a, signature.s());

        use curve25519_dalek::traits::IsIdentity;

        if (recomputed_r - signature.r())
            .mul_by_cofactor()
            .is_identity()
        {
            Ok(())
        } else {
            Err(SignatureError::InvalidSignature)
        }
    }
```

**File:** packages/ic-ed25519/tests/tests.rs (L517-545)
```rust
#[test]
fn public_key_accepts_but_can_detect_keys_with_torsion_component() {
    const WITH_TORSION: [[u8; 32]; 18] = [
        hex!("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa"),
        hex!("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85"),
        hex!("868b1e2248079aa8e24834a827ae8892ed0c826f87c897893cefffce3ac15242"),
        hex!("539903bdd44ecf43aa8ddcb730b1170be7879eab807b2f845754aa07001985bf"),
        hex!("5f44d0277fa2916ae1c7900ad094cff286a8163ee3aa20b4afe2ba91785389d6"),
        hex!("67867e99109b36830205573bcf3875f947ee473dc0d562786c7240ff8941d04d"),
        hex!("67fbbe649a6b8337006f8a2778e79d4f4e8e9c0a7042836eeaa60cb118e9841b"),
        hex!("dda5020fbe04b0ba7449157945718dfe20299f697b39681b03a5d0bec279ffae"),
        hex!("872d3823dcc001e354b09d618c70b2658cc3700c097514ae125cd14704c35a20"),
        hex!("92507296f36dd62d42b7e1306b99d02ffe19dea76f69cdaaf7211ce7f6c24fb9"),
        hex!("b93d302d6a2d629dee6e1415a00651c20e44c2545feb1914d7d41e4eecead522"),
        hex!("f41202b41dcda6410ffd5b8b5cd492b98986b60964d2f04aa1d963cdee64b7b0"),
        hex!("97766a5f4da3bb231935496300946d60bfbe04491750d1e23c4c8eceded274f4"),
        hex!("ae7ab64ec5821986bed36f98d4135cc047c9630c39b61b5f755678f818804eac"),
        hex!("05dd133d881cc14005f3cca6f5e759a8c7ea0bbfcef222e15bce904c70a4851b"),
        hex!("02ce23d0c026d9c95aecc36d5f40d7b7f505e29cad9c2014afd1f467ea15cf40"),
        hex!("ef164a8acaf9fde87b8dffb1b355f3dcefb857d76842720aefc1bfe26a0d9f2e"),
        hex!("4c95b17aa3870017da2b9e62d09689a8e9bb12a605093cba2fc2df02fde2fdbf"),
    ];

    for nc in &WITH_TORSION {
        let k = PublicKey::deserialize_raw(nc).unwrap();
        assert!(!k.is_torsion_free());
        assert!(k.is_canonical());
    }
}
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/cose/src/lib.rs (L188-200)
```rust
        match x {
            serde_cbor::Value::Bytes(x) => {
                let pk = ic_ed25519::PublicKey::deserialize_raw(x).map_err(|_| {
                    CosePublicKeyParseError::MalformedPublicKey(AlgorithmId::Ed25519)
                })?;

                let der = pk.serialize_rfc8410_der();
                Ok(Self::Ed25519(der))
            }
            _ => Err(CosePublicKeyParseError::MalformedPublicKey(
                AlgorithmId::Ed25519,
            )),
        }
```

**File:** rs/crypto/standalone-sig-verifier/src/sign_utils.rs (L50-62)
```rust
    let (key, algorithm_id, content_type) = if pkix_algo_id == ed25519_algorithm_identifier() {
        (
            ic_ed25519::PublicKey::deserialize_rfc8410_der(bytes)
                .map_err(|e| CryptoError::MalformedPublicKey {
                    algorithm: AlgorithmId::Ed25519,
                    key_bytes: Some(bytes.to_vec()),
                    internal_error: format!("{:?}", e),
                })?
                .serialize_raw()
                .to_vec(),
            AlgorithmId::Ed25519,
            KeyBytesContentType::Ed25519PublicKeyDer,
        )
```

**File:** rs/crypto/standalone-sig-verifier/src/lib.rs (L20-44)
```rust
        AlgorithmId::Ed25519 => {
            let pk = ic_ed25519::PublicKey::deserialize_raw(pk_bytes).map_err(|e| {
                CryptoError::MalformedPublicKey {
                    algorithm: AlgorithmId::Ed25519,
                    key_bytes: Some(pk_bytes.to_vec()),
                    internal_error: e.to_string(),
                }
            })?;

            if sig.len() != ic_ed25519::SIGNATURE_BYTES {
                return Err(CryptoError::MalformedSignature {
                    algorithm: AlgorithmId::Ed25519,
                    sig_bytes: sig.to_vec(),
                    internal_error: "Invalid length".to_string(),
                });
            }

            pk.verify_signature(msg, sig)
                .map_err(|e| CryptoError::SignatureVerification {
                    algorithm: AlgorithmId::Ed25519,
                    public_key_bytes: pk.serialize_raw().to_vec(),
                    sig_bytes: sig.to_vec(),
                    internal_error: e.to_string(),
                })
        }
```

**File:** rs/validator/src/ingress_validation.rs (L625-632)
```rust
// Verifies that the user id matches the public key.  Returns an error if not.
fn validate_user_id(sender_pubkey: &[u8], id: &UserId) -> Result<(), RequestValidationError> {
    if id.get_ref() == &PrincipalId::new_self_authenticating(sender_pubkey) {
        Ok(())
    } else {
        Err(UserIdDoesNotMatchPublicKey(*id, sender_pubkey.to_vec()))
    }
}
```

**File:** rs/validator/src/ingress_validation.rs (L659-669)
```rust
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
```

**File:** rs/validator/src/webauthn.rs (L12-47)
```rust
pub(crate) fn validate_webauthn_sig(
    verifier: &dyn IngressSigVerifier,
    webauthn_sig: &WebAuthnSignature,
    signable: &impl Signable,
    public_key: &UserPublicKey,
) -> Result<(), String> {
    let basic_sig = basic_sig_from_webauthn_sig(webauthn_sig, public_key.algorithm_id)?;

    let envelope = match WebAuthnEnvelope::try_from(webauthn_sig) {
        Ok(envelope) => envelope,
        Err(err) => {
            return Err(format!("WebAuthn envelope creation failed: {err}"));
        }
    };

    // Verify the signature signs the `WebAuthnEnvelope` provided.
    verifier
        .verify_basic_sig_by_public_key(&BasicSigOf::from(basic_sig.clone()), &envelope, public_key)
        .map_err(|e| {
            format!(
                "Verifying signature failed. signature: {:?}; envelope: {:?}; public_key: {}. Error: {}",
                basic_sig, envelope.clone(), public_key, e
            )
        })?;

    // The challenge in the webauthn envelope must match signed bytes.
    let signed_bytes = signable.as_signed_bytes();
    if envelope.challenge() != signed_bytes.as_slice() {
        Err(format!(
            "Challenge in webauthn is {:?} while it is expected to be {:?}",
            envelope.challenge(),
            signed_bytes,
        ))
    } else {
        Ok(())
    }
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/ed25519/src/api.rs (L97-103)
```rust
pub fn verify_public_key(pk: &types::PublicKeyBytes) -> bool {
    if let Ok(pk) = ic_ed25519::PublicKey::deserialize_raw(&pk.0) {
        pk.is_torsion_free()
    } else {
        false
    }
}
```
