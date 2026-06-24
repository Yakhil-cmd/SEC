Now I have the complete picture. Let me synthesize all findings.

---

### Title
Ed25519 Small-Order/Identity Public Key Accepted in Ingress Validation Enables Universal Signature Forgery for Derived Principal — (`packages/ic-ed25519/src/lib.rs`, `rs/crypto/standalone-sig-verifier/src/lib.rs`, `rs/validator/src/ingress_validation.rs`)

### Summary

The ingress signature validation pipeline accepts small-order Ed25519 public keys (including `[0u8; 32]`, a point of order 8, and the identity `[1, 0, ..., 0]`) without any subgroup check. The custom ZIP215 `verify_signature` implementation uses a cofactor check (`mul_by_cofactor().is_identity()`), which is algebraically satisfied for **any** message when the public key has order dividing 8 and the forged signature is `(R=identity, S=0)`. An unprivileged attacker can construct such a key, derive its self-authenticating principal, and submit ingress messages that pass all validation as that principal.

### Finding Description

**Step 1 — `Signature::from_slice` accepts `(R=identity, S=0)`**

The IC's custom `Signature` type in `packages/ic-ed25519/src/lib.rs` parses R by decompressing the raw bytes and S via `Scalar::from_canonical_bytes`: [1](#0-0) 

- `CompressedEdwardsY([1,0,...,0]).decompress()` succeeds — the identity is a valid curve point.
- `Scalar::from_canonical_bytes([0u8;32])` succeeds — S=0 is a valid canonical scalar.

No check rejects S=0 or R=identity.

**Step 2 — `verify_signature` passes for any small-order public key with `(R=identity, S=0)`**

The verification equation is: [2](#0-1) 

With public key A of order dividing 8 (e.g., `[0u8;32]` has order 8, confirmed by test), S=0, R=identity:

```
recomputed_r = S·B + k·(−A) = 0·B + k·(−A) = k·(−A)
(recomputed_r − R)·8 = (k·(−A) − identity)·8
                     = k·(−A)·8 − identity·8
                     = k·(8·(−A)) − identity
                     = k·identity − identity   [since ord(A)|8 ⟹ 8·A = identity]
                     = identity − identity = identity  ✓
```

`is_identity()` returns `true` → `Ok(())` for **any** message.

**Step 3 — `deserialize_raw` accepts small-order keys without subgroup check** [3](#0-2) 

`VerifyingKey::from_bytes` only checks the point is on the curve, not that it is torsion-free. The test explicitly confirms `[0u8;32]` is accepted and is a small-order point: [4](#0-3) 

**Step 4 — The ingress path never calls `verify_public_key`**

`verify_public_key` (which calls `is_torsion_free()` and would reject small-order keys) is **not** called during ingress validation: [5](#0-4) 

The actual ingress path goes through `verify_basic_sig_by_public_key` in the standalone verifier: [6](#0-5) 

Which calls `deserialize_raw` (no torsion check) then `verify_signature` (accepts the forgery).

**Step 5 — The ingress validation chain has no guard** [7](#0-6) 

`validate_user_id_and_signature` → `validate_user_id` (checks sender = `SHA224(DER(pk)) || 0x02`) → `validate_signature` → `validate_signature_plain` → `verify_basic_sig_by_public_key`. No step rejects small-order keys.

### Impact Explanation

An attacker can:
1. Choose `pk_raw = [0u8; 32]` (small-order point, order 8)
2. DER-encode it (standard Ed25519 DER prefix + 32 bytes)
3. Derive the self-authenticating principal: `SHA224(DER(pk)) || 0x02`
4. Construct forged signature: `R = [1,0,...,0]` (identity, 32 bytes) ‖ `S = [0u8;32]` (64 bytes)
5. Submit any ingress message (update call, query, read_state) with this sender, public key, and forged signature
6. All validation passes

The attacker can execute calls as the specific principal derived from the small-order key. This is a fixed, publicly computable principal. The attacker cannot impersonate arbitrary users — only this one specific principal.

### Likelihood Explanation

- No preconditions, no privileged access, no key material needed.
- Fully constructible offline from public knowledge.
- Concrete and locally testable.
- The forged principal is deterministic and publicly known, limiting practical damage unless a canister has granted it permissions.

### Recommendation

In `verify_basic_sig_by_public_key` (or in `PublicKey::deserialize_raw` when used for signature verification), add a subgroup check equivalent to `verify_public_key`:

```rust
if !pk.is_torsion_free() {
    return Err(CryptoError::MalformedPublicKey { ... });
}
```

This is already implemented in `verify_public_key` and in node key validation: [8](#0-7) 

The same guard must be applied in the ingress signature verification path.

### Proof of Concept

```rust
// pk = [0u8; 32] — small-order point (order 8), passes deserialize_raw
let pk_raw = [0u8; 32];
let pk_der = ic_ed25519::PublicKey::convert_raw32_to_der(pk_raw);

// Derive self-authenticating principal
let principal = PrincipalId::new_self_authenticating(&pk_der);

// Forged signature: R = identity [1,0,...,0], S = 0
let mut forged_sig = [0u8; 64];
forged_sig[0] = 1; // R = canonical identity encoding

// verify_basic_sig_by_public_key(Ed25519, any_msg, &forged_sig, &pk_raw) → Ok(())
// Submit ingress with sender=principal, signer_pubkey=pk_der, signature=forged_sig → accepted
```

### Citations

**File:** packages/ic-ed25519/src/lib.rs (L449-471)
```rust
    fn from_slice(signature: &[u8]) -> Result<Self, SignatureError> {
        if signature.len() != SIGNATURE_BYTES {
            return Err(SignatureError::InvalidLength);
        }

        let (r, r_bytes) = {
            let mut r_bytes = [0_u8; 32];
            r_bytes.copy_from_slice(&signature[..32]);
            let r = CompressedEdwardsY(r_bytes)
                .decompress()
                .ok_or(SignatureError::InvalidSignature)?;

            (r, r_bytes)
        };

        let s = {
            let mut s_bytes = [0_u8; 32];
            s_bytes.copy_from_slice(&signature[32..]);
            Option::<Scalar>::from(Scalar::from_canonical_bytes(s_bytes))
                .ok_or(SignatureError::InvalidSignature)?
        };

        Ok(Self { r, r_bytes, s })
```

**File:** packages/ic-ed25519/src/lib.rs (L619-631)
```rust
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

**File:** rs/crypto/internal/crypto_lib/basic_sig/ed25519/src/api/tests.rs (L342-355)
```rust
    fn should_fail_public_key_verification_if_point_has_small_order() {
        let pubkey_with_small_order = {
            let pubkey_with_order_8 = PublicKeyBytes([0; 32]);
            assert!(
                CompressedEdwardsY(pubkey_with_order_8.0)
                    .decompress()
                    .expect("pubkey cannot be decompressed")
                    .is_small_order()
            );
            pubkey_with_order_8
        };

        assert!(!verify_public_key(&pubkey_with_small_order));
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

**File:** rs/validator/src/ingress_validation.rs (L842-876)
```rust
fn validate_user_id_and_signature<R: RootOfTrustProvider>(
    ingress_signature_verifier: &dyn IngressSigVerifier,
    sender: &UserId,
    message_id: &MessageId,
    signature: Option<&UserSignature>,
    current_time: Time,
    root_of_trust_provider: &R,
) -> Result<CanisterIdSet, RequestValidationError>
where
    R::Error: std::error::Error,
{
    match signature {
        None => {
            if sender.get().is_anonymous() {
                return Ok(CanisterIdSet::all());
            }
            Err(MissingSignature(*sender))
        }
        Some(signature) => {
            if sender.get().is_anonymous() {
                Err(AnonymousSignatureNotAllowed)
            } else {
                let sender_pubkey = &signature.signer_pubkey;
                validate_user_id(sender_pubkey, sender).and_then(|()| {
                    validate_signature(
                        ingress_signature_verifier,
                        message_id,
                        signature,
                        current_time,
                        root_of_trust_provider,
                    )
                })
            }
        }
    }
```

**File:** rs/crypto/node_key_validation/src/lib.rs (L233-240)
```rust
        let pk = ic_ed25519::PublicKey::deserialize_raw(&pk_proto.key_value)
            .map_err(|e| invalid_node_signing_key_error(format!("{:?}", e)))?;

        if !pk.is_torsion_free() {
            return Err(invalid_node_signing_key_error(
                "has torsion component".to_string(),
            ));
        }
```
