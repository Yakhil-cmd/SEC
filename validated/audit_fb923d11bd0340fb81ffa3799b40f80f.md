The call sequence is fully traceable in the code. Let me map it precisely.

The call chain is fully traceable. Here is the precise analysis.

---

### Title
Pre-Bounds-Check Allocation in RSA COSE Key Parsing via `spki_from_components` — (`rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs`)

### Summary

`RsaPublicKey::from_components` calls `spki_from_components` — which allocates `BigInt` objects and two DER-encoded buffers proportional to the raw `n` bytes — before the modulus size check in `from_der_spki` is ever reached. An unauthenticated sender can embed an oversized RSA modulus in a COSE-encoded public key inside an ingress message and trigger this allocation path on every replica that validates the request.

### Finding Description

The call sequence is:

```
HTTP POST /api/v2/canister/{id}/call
  → ingress size check (call.rs:302)          ← total message ≤ 2 MB passes
  → validator.validate_request
    → validate_user_id_and_signature
      → public_key_from_bytes / user_public_key_from_bytes (sign_utils.rs:88)
        → cose::parse_cose_public_key
          → CosePublicKey::parse_rsa_pkcs1_sha256 (cose/src/lib.rs:221)
            → RsaPublicKey::from_components (rsa_pkcs1/src/lib.rs:74)
              → spki_from_components            ← ALLOCATES HERE
                → BigInt::from_bytes_be(n)      ← ~|n| bytes
                → to_der(Sequence[n,e])         ← ~|n| bytes
                → to_der(SPKI wrapper)          ← ~|n| bytes
              → from_der_spki                   ← SIZE CHECK HERE (too late)
```

In `spki_from_components`: [1](#0-0) 

`BigInt::from_bytes_be(Sign::Plus, n)` constructs a heap-allocated big integer from the full raw byte slice before any length guard. `to_der` then serializes it into a new `Vec<u8>`, and a second `to_der` call wraps it in the SPKI structure — three allocations each proportional to `|n|`.

The size guard only fires in `from_der_spki` after `spki_from_components` has already returned: [2](#0-1) [3](#0-2) 

The COSE parser extracts `n` as a raw `Vec<u8>` from the CBOR map and passes it directly: [4](#0-3) 

### Impact Explanation

The ingress message size cap is `MAX_INGRESS_BYTES_PER_MESSAGE_APP_SUBNET = 2 MB` (app subnets) and `3.5 MB` (NNS): [5](#0-4) 

This cap is enforced before `validate_request` is called: [6](#0-5) 

So the maximum attacker-controlled `|n|` is bounded at roughly 2 MB. The three allocations in `spki_from_components` add approximately **3× |n|** of heap pressure — ~6 MB per request — beyond what CBOR parsing already allocated. A single request is not catastrophic, but the validation runs in a `spawn_blocking` thread pool and is stateless, so an attacker can pipeline many concurrent requests. At, say, 100 concurrent requests each with a ~2 MB `n`, the transient heap overhead is ~600 MB. This is a real but bounded resource amplification, not a direct OOM crash.

The claim of "8193+ bits" (1025 bytes) is trivially within the 2 MB limit. The claim of "100 MB" is not reachable due to the ingress size cap.

### Likelihood Explanation

The path is fully unauthenticated — the public key is parsed as part of signature validation, which must happen before the sender's identity is established. No privileged role is required. The attack is reproducible with a single crafted HTTP POST. The ingress size limit caps the per-request impact, but the allocation-before-check invariant is concretely violated.

### Recommendation

Add an explicit byte-length guard in `from_components` before calling `spki_from_components`:

```rust
pub fn from_components(e: &[u8], n: &[u8]) -> CryptoResult<Self> {
    // MAXIMUM_RSA_KEY_SIZE is in bits; n is big-endian bytes
    if n.len() > Self::MAXIMUM_RSA_KEY_SIZE / 8 + 1 {
        return Err(CryptoError::MalformedPublicKey {
            algorithm: AlgorithmId::RsaSha256,
            key_bytes: None,
            internal_error: "RSA modulus too large".to_string(),
        });
    }
    let der = Self::spki_from_components(e, n)?;
    Self::from_der_spki(&der)
}
```

This makes the check O(1) and eliminates all proportional allocation for oversized inputs. [7](#0-6) 

### Proof of Concept

```rust
// Craft a COSE RSA map with n = 2 MB of zeros
let n = vec![0x01u8; 2_000_000]; // 16M-bit "modulus"
let e = vec![0x01, 0x00, 0x01];  // 65537
// Encode as CBOR map: {1: 3, 3: -257, -1: n, -2: e}
// POST to /api/v2/canister/<id>/call with sender_pubkey = DER-wrapped COSE key
// Measure peak RSS before the MalformedPublicKey error is returned
```

Peak allocation before rejection: ~6 MB per request. At 100 concurrent requests: ~600 MB transient heap pressure.

---

**Verdict: Valid finding.** The invariant is concretely violated — allocations proportional to attacker-controlled input occur before bounds checking — and the path is reachable from an unauthenticated ingress call. The practical impact is bounded by the 2 MB ingress cap, making this a resource-amplification issue rather than a direct OOM crash, but the root cause is real and the fix is straightforward.

### Citations

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L37-38)
```rust
    pub const MINIMUM_RSA_KEY_SIZE: usize = 2048;
    pub const MAXIMUM_RSA_KEY_SIZE: usize = 8192;
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L49-56)
```rust
        let n = ASN1Block::Integer(0, BigInt::from_bytes_be(Sign::Plus, n));
        let e = ASN1Block::Integer(0, BigInt::from_bytes_be(Sign::Plus, e));
        let blocks = vec![n, e];

        let pkcs1 =
            to_der(&ASN1Block::Sequence(0, blocks)).map_err(|e| CryptoError::InvalidArgument {
                message: format!("{e:?}"),
            })?;
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L73-76)
```rust
    pub fn from_components(e: &[u8], n: &[u8]) -> CryptoResult<Self> {
        let der = Self::spki_from_components(e, n)?;
        Self::from_der_spki(&der)
    }
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs (L128-134)
```rust
        if modulus_bits > Self::MAXIMUM_RSA_KEY_SIZE {
            return Err(CryptoError::MalformedPublicKey {
                algorithm: AlgorithmId::RsaSha256,
                key_bytes: Some(bytes.to_vec()),
                internal_error: "RSA public key too large to accept".to_string(),
            });
        }
```

**File:** rs/crypto/internal/crypto_lib/basic_sig/cose/src/lib.rs (L219-224)
```rust
        match (e, n) {
            (serde_cbor::Value::Bytes(e), serde_cbor::Value::Bytes(n)) => {
                let key = RsaPublicKey::from_components(e, n).map_err(|_| {
                    CosePublicKeyParseError::MalformedPublicKey(AlgorithmId::RsaSha256)
                })?;
                let der = key.as_der().to_vec();
```

**File:** rs/limits/src/lib.rs (L85-86)
```rust
pub const MAX_INGRESS_BYTES_PER_MESSAGE_APP_SUBNET: u64 = 2 * MEGABYTE;
pub const MAX_INGRESS_BYTES_PER_MESSAGE_NNS_SUBNET: u64 = 3 * MEGABYTE + 512 * KILOBYTE;
```

**File:** rs/http_endpoints/public/src/call.rs (L302-312)
```rust
        if msg.count_bytes() > ingress_registry_settings.max_ingress_bytes_per_message {
            Err(HttpError {
                status: StatusCode::PAYLOAD_TOO_LARGE,
                message: format!(
                    "Request {} is too large. Message byte size {} is larger than the max allowed {}.",
                    message_id,
                    msg.count_bytes(),
                    ingress_registry_settings.max_ingress_bytes_per_message
                ),
            })?;
        }
```
