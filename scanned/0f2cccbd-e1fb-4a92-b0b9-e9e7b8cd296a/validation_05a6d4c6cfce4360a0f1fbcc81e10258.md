### Title
Unbounded RSA Public Exponent Enables Algorithmic DoS via Slow Modular Exponentiation in WebAuthn Ingress Validation — (`rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs`)

---

### Summary

`RsaPublicKey::from_der_spki` enforces bounds on the RSA **modulus** size (2048–8192 bits) and checks that both `n` and `e` are odd, but imposes **no upper bound on the public exponent `e`**. An unprivileged attacker can craft a COSE RSA WebAuthn key with a near-modulus-sized exponent (e.g., 8191 bits), submit ingress messages signed with it, and force each replica ingress-validation thread to perform an astronomically expensive modular exponentiation during `verify_pkcs1_sha256`, causing a replica-level DoS.

---

### Finding Description

`from_der_spki` in `rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs` explicitly adds three guards that the underlying `rsa` crate omits: [1](#0-0) 

1. **Exponent must be odd** (line 100)
2. **Modulus must be odd** (line 109)
3. **Modulus must be 2048–8192 bits** (lines 118–134)

There is **no corresponding guard on `e.bits()`**. The exponent bytes flow in from the COSE CBOR field `COSE_PARAM_RSA_E` with no length check: [2](#0-1) 

`from_components` simply DER-encodes whatever bytes are supplied and delegates to `from_der_spki`: [3](#0-2) 

`verify_pkcs1_sha256` then calls the `rsa` crate's `verify`, which unconditionally computes `sig^e mod n` (square-and-multiply, O(log e) multiplications of O(n²) cost each): [4](#0-3) 

The ingress validation path reaches this code for any WebAuthn RSA key: [5](#0-4) 

---

### Impact Explanation

RSA verification time scales linearly with the bit-length of `e`. With `e = 65537` (17 bits), verification requires ~17 modular squarings. With `e` at 8191 bits (the maximum allowed given the 8192-bit modulus cap), verification requires ~8191 modular squarings — roughly **480× slower**. Each squaring operates on 8192-bit integers. An attacker submitting a sustained stream of ingress messages with such keys can saturate the ingress-validation thread pool, stalling all ingress processing on the targeted replica(s) and causing ledger/canister call timeouts.

---

### Likelihood Explanation

The attack is fully unprivileged: any principal can register a self-authenticating identity derived from an arbitrary COSE RSA key and submit ingress messages. No special access, key material, or network position is required. The crafted key passes all existing validation gates. The only cost to the attacker is constructing the CBOR payload and sending HTTP requests to the replica's `/api/v2/canister/.../call` endpoint.

---

### Recommendation

Add an explicit upper bound on the exponent bit-length in `from_der_spki`, immediately after the existing oddness check. The standard WebAuthn/FIDO2 practice is to require `e ≤ 2^32 − 1` (i.e., at most 32 bits). A conservative but safe limit is:

```rust
// After the existing oddness check on e:
let exponent_bits = parsed.e().bits();
if exponent_bits > 32 {
    return Err(CryptoError::MalformedPublicKey {
        algorithm: AlgorithmId::RsaSha256,
        key_bytes: Some(bytes.to_vec()),
        internal_error: "RSA public exponent is too large".to_string(),
    });
}
```

This mirrors the pattern already used for the modulus size check and is consistent with FIDO2 Authenticator requirements (which mandate `e = 65537`).

---

### Proof of Concept

```rust
// Craft a 2048-bit n (any odd 2048-bit integer) and an 8191-bit odd e.
// Both pass from_der_spki: n is 2048 bits (in range), e is odd.
// Then call verify_pkcs1_sha256 with a 256-byte signature payload.
// Measure wall-clock time vs. e = 65537.

let n = /* 2048-bit odd integer */;
let e_small = BigUint::from(65537u32);
let e_large = (BigUint::one() << 8191u32) - BigUint::one(); // 8191-bit odd number

let key_small = RsaPublicKey::from_components(&e_small.to_bytes_be(), &n).unwrap();
let key_large = RsaPublicKey::from_components(&e_large.to_bytes_be(), &n).unwrap();
// key_large construction succeeds — no exponent size check.

let sig = vec![0u8; 256]; // any 256-byte payload triggers full exponentiation
let msg = b"test";

let t0 = Instant::now();
let _ = key_small.verify_pkcs1_sha256(msg, &sig);
println!("e=65537: {:?}", t0.elapsed());

let t1 = Instant::now();
let _ = key_large.verify_pkcs1_sha256(msg, &sig);
println!("e=2^8191-1: {:?}", t1.elapsed()); // expected: hundreds of ms to seconds
```

The ingress path that reaches this code for a WebAuthn RSA key is:

`/api/v2/canister/.../call` → `validate_signature` → `validate_webauthn_sig` → `verify_pkcs1_sha256` [6](#0-5)

### Citations

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L73-76)
```rust
    pub fn from_components(e: &[u8], n: &[u8]) -> CryptoResult<Self> {
        let der = Self::spki_from_components(e, n)?;
        Self::from_der_spki(&der)
    }
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L99-134)
```rust
        // RustCrypto/rsa does not verify that the public exponent is odd
        if parsed.e() % &two == BigUint::zero() {
            return Err(CryptoError::MalformedPublicKey {
                algorithm: AlgorithmId::RsaSha256,
                key_bytes: Some(bytes.to_vec()),
                internal_error: "RSA public exponent is invalid".to_string(),
            });
        }

        // RustCrypto/rsa does not verify that the public modulus is odd
        if parsed.n() % &two == BigUint::zero() {
            return Err(CryptoError::MalformedPublicKey {
                algorithm: AlgorithmId::RsaSha256,
                key_bytes: Some(bytes.to_vec()),
                internal_error: "RSA public modulus is invalid".to_string(),
            });
        }

        // RustCrypto/rsa does not check if the modulus is valid size
        let modulus_bits = parsed.n().bits();

        if modulus_bits < Self::MINIMUM_RSA_KEY_SIZE {
            return Err(CryptoError::MalformedPublicKey {
                algorithm: AlgorithmId::RsaSha256,
                key_bytes: Some(bytes.to_vec()),
                internal_error: "RSA public key too small to accept".to_string(),
            });
        }

        if modulus_bits > Self::MAXIMUM_RSA_KEY_SIZE {
            return Err(CryptoError::MalformedPublicKey {
                algorithm: AlgorithmId::RsaSha256,
                key_bytes: Some(bytes.to_vec()),
                internal_error: "RSA public key too large to accept".to_string(),
            });
        }
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L152-167)
```rust
    pub fn verify_pkcs1_sha256(&self, message: &[u8], signature: &[u8]) -> CryptoResult<()> {
        let digest = Sha256::hash(message);

        match &self
            .key
            .verify(Pkcs1v15Sign::new::<sha2::Sha256>(), &digest, signature)
        {
            Ok(_) => Ok(()),
            Err(e) => Err(CryptoError::SignatureVerification {
                algorithm: AlgorithmId::RsaSha256,
                public_key_bytes: self.as_der().to_vec(),
                sig_bytes: signature.to_vec(),
                internal_error: format!("{e:?}"),
            }),
        }
    }
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/cose/src/lib.rs (L219-225)
```rust
        match (e, n) {
            (serde_cbor::Value::Bytes(e), serde_cbor::Value::Bytes(n)) => {
                let key = RsaPublicKey::from_components(e, n).map_err(|_| {
                    CosePublicKeyParseError::MalformedPublicKey(AlgorithmId::RsaSha256)
                })?;
                let der = key.as_der().to_vec();
                Ok(Self::RsaPkcs1v15Sha256(der))
```

**File:** rs/validator/src/ingress_validation.rs (L635-669)
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
```
