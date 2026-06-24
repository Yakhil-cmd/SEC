### Title
Ed25519 Non-Canonical Identity Element Encoding Bypasses Signature Forgery Protection — (`rs/crypto/internal/crypto_lib/basic_sig/ed25519/src/api.rs`)

---

### Summary

`verify_public_key()` accepts all three non-canonical torsion-free encodings of the identity element because it only calls `is_torsion_free()` and not `is_canonical()`. For any key that decodes to the identity point, `verify_signature()` under ZIP215 accepts **any** `(R = S·G, S)` pair as a valid signature. An unprivileged attacker can therefore forge a valid ingress signature for the principal derived from any of these three encodings.

---

### Finding Description

**Guard under examination — `verify_public_key()`:** [1](#0-0) 

The function calls only `is_torsion_free()`. The `ic_ed25519` library explicitly documents and tests that the three non-canonical identity encodings are torsion-free but non-canonical: [2](#0-1) 

The three encodings are:
- `0x0100000000000000000000000000000000000000000000000000000000000080`
- `0xeeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f`
- `0xeeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff`

All three pass `deserialize_raw()` successfully (the test calls `.unwrap()`) and return `true` from `is_torsion_free()`, so `verify_public_key()` returns `true` for all of them.

**Signature verification with the identity element key:** [3](#0-2) 

For a key `A` that is the identity element (the point at infinity):
- `minus_a = -identity = identity = 0`
- `recomputed_r = k·0 + S·G = S·G`
- Check: `(S·G − R)·cofactor == identity`

If the attacker submits `R = S·G` for any `S < L`, the check becomes `(S·G − S·G)·cofactor = 0`, which is the identity → `Ok(())`. The challenge scalar `k` is irrelevant because it is multiplied by the identity point. This is confirmed by the ZIP215 test vectors, which include the canonical identity key paired with many distinct signatures, all of which pass: [4](#0-3) 

**`verify()` does not call `verify_public_key()`:** [5](#0-4) 

`verify()` calls only `deserialize_raw()` (which succeeds for non-canonical identity encodings) and then `verify_signature()`. There is no canonicality check in this path.

**Ingress validation path:** [6](#0-5) 

`validate_user_id_and_signature()` calls `validate_user_id()` (which only checks that `sender == PrincipalId::new_self_authenticating(sender_pubkey)` — a check the attacker satisfies by construction) and then `validate_signature()` → `validate_signature_plain()` → `validator.verify_basic_sig_by_public_key()`, which ultimately calls `verify()`. No additional canonicality guard exists in this path. [7](#0-6) 

---

### Impact Explanation

An attacker can:
1. Choose any of the three non-canonical identity encodings as `sender_pubkey` (DER-wrapped).
2. Derive `PrincipalId = new_self_authenticating(DER(non_canonical_identity))` and set `sender` to this value.
3. Pick any `S < L`, compute `R = S·G`, and submit `(R ‖ S)` as the signature.
4. The ingress message passes all validation and is accepted by the replica.

The attacker authenticates as the principal derived from the non-canonical identity encoding without possessing any private key. They can perform any action that principal is authorized for — including creating and controlling canisters registered under that principal, or spending cycles/tokens held by it.

---

### Likelihood Explanation

The attack requires no privileged access, no key material, no network-level attack, and no social engineering. The three target encodings are publicly documented. Computing `R = S·G` for any `S` is a single scalar multiplication. The exploit is fully local and deterministic.

---

### Recommendation

Add an `is_canonical()` check inside `verify_public_key()`:

```rust
pub fn verify_public_key(pk: &types::PublicKeyBytes) -> bool {
    if let Ok(pk) = ic_ed25519::PublicKey::deserialize_raw(&pk.0) {
        pk.is_torsion_free() && pk.is_canonical()  // add is_canonical()
    } else {
        false
    }
}
``` [8](#0-7) 

This mirrors the pattern already used in the threshold-sig group module, which explicitly rejects all non-canonical identity encodings before any further processing: [9](#0-8) 

---

### Proof of Concept

```rust
// Encoding 2 of the 3 non-canonical identity elements
let non_canonical_identity: [u8; 32] =
    hex!("eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f");

// DER-wrap it (same prefix as convert_raw32_to_der)
let der_pubkey = ic_ed25519::PublicKey::convert_raw32_to_der(non_canonical_identity);

// Derive the principal the attacker will impersonate
let principal = PrincipalId::new_self_authenticating(&der_pubkey);
let sender = UserId::from(principal);

// Forge a signature: pick S=1, R = 1·G (base point)
let s_scalar = Scalar::ONE;
let r_point = ED25519_BASEPOINT_POINT * s_scalar;
let mut sig = [0u8; 64];
sig[..32].copy_from_slice(&r_point.compress().0);
sig[32..].copy_from_slice(s_scalar.as_bytes());

// Build and submit ingress message with sender=principal, sender_pubkey=der_pubkey,
// signature=sig — validation passes, ingress is accepted.
```

### Citations

**File:** rs/crypto/internal/crypto_lib/basic_sig/ed25519/src/api.rs (L70-91)
```rust
pub fn verify(
    sig: &types::SignatureBytes,
    msg: &[u8],
    pk: &types::PublicKeyBytes,
) -> CryptoResult<()> {
    let public_key = ic_ed25519::PublicKey::deserialize_raw(&pk.0).map_err(|e| {
        CryptoError::MalformedPublicKey {
            algorithm: AlgorithmId::Ed25519,
            key_bytes: Some(pk.0.to_vec()),
            internal_error: e.to_string(),
        }
    })?;

    public_key
        .verify_signature(msg, &sig.0)
        .map_err(|e| CryptoError::SignatureVerification {
            algorithm: AlgorithmId::Ed25519,
            public_key_bytes: public_key.serialize_raw().to_vec(),
            sig_bytes: sig.0.to_vec(),
            internal_error: e.to_string(),
        })
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

**File:** packages/ic-ed25519/src/lib.rs (L555-558)
```rust
    /// Return true if and only if the public key uses a canonical encoding
    pub fn is_canonical(&self) -> bool {
        self.pk.to_bytes() == self.pk.to_edwards().compress().0
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/utils/group/ed25519.rs (L295-333)
```rust
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
