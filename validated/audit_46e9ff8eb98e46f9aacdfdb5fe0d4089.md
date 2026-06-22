The code is fully readable. Here is the complete analysis:

**Validation logic in `from_der_spki`** (lines 97–134):

```
1. e % 2 == 0  → reject (even exponent)
2. n % 2 == 0  → reject (even modulus)
3. n.bits() < 2048 → reject
4. n.bits() > 8192 → reject
```

For e=1: `1 % 2 = 1 ≠ 0` → **passes check #1**. No check for `e >= 3` exists anywhere in the codebase. The comment at line 99 explicitly states the rsa crate doesn't check oddness — meaning the rsa crate's DER parser adds no exponent validation either.

---

### Title
RSA public exponent e=1 accepted by `from_der_spki`, enabling universal signature forgery — (`rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs`)

### Summary
`RsaPublicKey::from_der_spki` only rejects **even** exponents. The value `e=1` is odd and passes all validation. With e=1, RSA verification computes `sig^1 mod n = sig`, so any attacker who knows the PKCS#1 v1.5 padded hash format can forge a valid signature for any message by submitting the padded hash itself as the signature.

### Finding Description
`from_der_spki` performs four checks on an RSA public key: [1](#0-0) 

The only exponent check is `e % 2 == 0` (reject even). Since `1 % 2 = 1`, e=1 is accepted. There is no guard for `e < 3` or any minimum exponent value. The comment at line 99 confirms the upstream `rsa` crate also does not validate the exponent, so no secondary check exists. [2](#0-1) 

The COSE RSA parser calls `RsaPublicKey::from_components(e, n)` which delegates directly to `from_der_spki`: [3](#0-2) 

`from_components` itself is just a thin wrapper: [4](#0-3) 

### Impact Explanation
With e=1, `verify_pkcs1_sha256` computes `sig^1 mod n = sig`. PKCS#1 v1.5 verification then checks whether `sig` equals the expected padded hash `0x00 0x01 [0xFF...] 0x00 [DigestInfo] SHA256(M)`. An attacker constructs this 256-byte value directly (it is fully deterministic from the message) and submits it as the "signature." Since `sig < n` for a 2048-bit key, `sig mod n = sig`, and verification succeeds unconditionally for any message. [5](#0-4) 

Attack path:
1. Attacker registers a WebAuthn principal with a COSE RSA key where `e = [0x01]` and `n` is any valid 2048-bit odd modulus.
2. Key passes `parse_cose_public_key → parse_rsa_pkcs1_sha256 → from_components → from_der_spki` and is stored as a valid `RsaSha256` key.
3. For any ingress message M, attacker computes the PKCS#1 v1.5 padded SHA-256 hash of M and submits it as the signature.
4. `verify_pkcs1_sha256` accepts it. The attacker's principal is authenticated for any message.

This enables unauthorized ICP ledger transfers, canister calls, and any other action gated by WebAuthn RSA authentication.

### Likelihood Explanation
The attack requires no privileged access, no key material, and no cryptographic computation beyond SHA-256 and PKCS#1 padding construction — both trivially available. The only prerequisite is the ability to register a WebAuthn key, which is the normal unprivileged user flow. The forged signature is deterministic and always succeeds.

### Recommendation
Add a minimum exponent check in `from_der_spki` immediately after the oddness check:

```rust
let three = BigUint::from_u8(3).expect("Unable to create 3 BigUint");
if parsed.e() < &three {
    return Err(CryptoError::MalformedPublicKey {
        algorithm: AlgorithmId::RsaSha256,
        key_bytes: Some(bytes.to_vec()),
        internal_error: "RSA public exponent is too small (must be >= 3)".to_string(),
    });
}
```

The standard minimum for RSA public exponents is 3 (or preferably 65537). The existing test `should_reject_invalid_rsa_pubkey_from_components` should be extended to assert `from_components(&[0x01], &valid_n).is_err()`. [6](#0-5) 

### Proof of Concept
```rust
use ic_crypto_internal_basic_sig_rsa_pkcs1::RsaPublicKey;

// Step 1: confirm e=1 is accepted
let valid_n = hex::decode("f214c6...04205").unwrap(); // any valid 2048-bit odd modulus
let e_one = vec![0x01u8];
let key = RsaPublicKey::from_components(&e_one, &valid_n);
assert!(key.is_ok(), "e=1 should be rejected but was accepted");

// Step 2: forge a signature for an arbitrary message
let key = key.unwrap();
let message = b"transfer 1000 ICP to attacker";
// Construct PKCS#1 v1.5 padded SHA-256 hash manually (256 bytes for 2048-bit key):
// 0x00 0x01 [0xFF * 202] 0x00 [DigestInfo prefix 19 bytes] [SHA256(message) 32 bytes]
let forged_sig = pkcs1v15_pad_sha256(message); // deterministic, no secret needed
assert!(key.verify_pkcs1_sha256(message, &forged_sig).is_ok(),
    "Forged signature accepted — authentication bypass confirmed");
```

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

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/tests/tests.rs (L22-32)
```rust
#[test]
fn should_reject_invalid_rsa_pubkey_from_components() {
    let valid_n = hex::decode("f214c6550cae299ad12538decc70726089538b4236af396399345c7bb7d92e53f4b5dbc9cffd57509fa91e9feba0d7838487a008853fb8ea96970daee66b5119a38fd7796773930bac05b837031f7f89b5bfd92d2ebde5c5eb0665460bf9477a82e7ca6d86c41a4f74c058890f4e5acbc448bf39dd008e914fbbad2c0eee4aa69adf17d923416400635c1420ba008855ca38dcd48c8373d7a80cc2e1745caa2864f7b28cbef5480a595ab1ee156b32017269595816e6cc4f4ad614fccbb013a9cee17c365f8caa23474d5b97f0f096802b73f042af076c88ff037cd0fc706d2392393a56ed58b4e5dbec844bfb91fb9601c64df10f7349b28950344611904205").unwrap();
    let valid_e = hex::decode("010001").unwrap();

    let small = hex::decode("E6C32A70B0445F5F62C87FB9F0BA8DDABEC25E9F").unwrap();
    let even = hex::decode("11000A").unwrap();

    assert!(RsaPublicKey::from_components(&even, &valid_n).is_err());
    assert!(RsaPublicKey::from_components(&valid_e, &small).is_err());
}
```
