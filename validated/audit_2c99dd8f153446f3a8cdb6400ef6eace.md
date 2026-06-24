Audit Report

## Title
Missing `is_canonical()` Check Allows Identity-Element TLS Certificate Registration and Boundary-Node Peer Impersonation — (`rs/crypto/node_key_validation/tls_cert_validation/src/lib.rs`)

## Summary

`verify_ed25519_public_key` checks only `is_torsion_free()` and never `is_canonical()`. The three non-canonical torsion-free encodings of the Ed25519 identity element pass this check, allowing a node operator to register a TLS certificate whose public key is the identity element. Under ZIP215 rules (used throughout `ic_ed25519`), the identity element accepts any signature `(R, S)` where `R = [S]B`, so a crafted self-signed certificate with the identity element as the public key also passes `verify_ed25519_signature` and is written to the registry. The boundary node's `TlsVerifier` then uses the registry public key (identity element) to authenticate the presented certificate, allowing any party to forge a passing `verify_server_cert` for that node.

## Finding Description

**Root cause — `verify_ed25519_public_key` omits `is_canonical()`**

`verify_ed25519_public_key` at `rs/crypto/node_key_validation/tls_cert_validation/src/lib.rs` lines 220–229 only rejects keys that fail `is_torsion_free()`: [1](#0-0) 

`is_canonical()` exists on `PublicKey` at `packages/ic-ed25519/src/lib.rs` lines 555–558 but is never called in this path: [2](#0-1) 

**Three non-canonical identity-element encodings are torsion-free**

The test suite at `packages/ic-ed25519/tests/tests.rs` lines 499–514 explicitly documents and confirms that all three non-canonical encodings of the identity element satisfy `is_torsion_free() == true` and `is_canonical() == false`: [3](#0-2) 

`PublicKey::deserialize_raw` accepts all three encodings without error (confirmed by the `.unwrap()` in the test).

**Self-signature passes under ZIP215**

`verify_ed25519_signature` calls `ic_ed25519::PublicKey::verify_signature`, which implements ZIP215: [4](#0-3) 

The ZIP215 verification equation at `packages/ic-ed25519/src/lib.rs` lines 709–727 computes `recomputed_r = [k](−A) + [S]B`. When `A = identity`, `−A = identity` and `[k](−A) = identity`, so `recomputed_r = [S]B`. The check reduces to `([S]B − R).mul_by_cofactor().is_identity()`. Choosing `S = 1`, `R = B` satisfies this for any message `M`: [5](#0-4) 

**Certificate is written to the registry**

`do_add_node` calls `valid_keys_from_payload` → `ValidNodePublicKeys::try_from` → `ValidTlsCertificate::try_from`. If `ValidTlsCertificate::try_from` returns `Ok`, the certificate is inserted into the registry via `make_crypto_tls_cert_key`: [6](#0-5) 

**Boundary node `TlsVerifier` uses the registry public key (identity element) to authenticate the presented certificate**

`TlsVerifier::verify_server_cert` at `rs/boundary_node/ic_boundary/src/tls_verify.rs` lines 107–129 extracts the public key from the registry certificate and calls `ic_ed25519::PublicKey::verify_signature` (ZIP215) on the presented certificate's self-signature. There is no byte-equality check between the presented and registry certificates in this path: [7](#0-6) 

If the registry certificate carries the identity element public key, any presented certificate whose self-signature is `(R=B, S=1)` passes `verify_server_cert`. The `CertificateVerify` message in the TLS handshake is subsequently verified against the presented certificate's public key; if that public key is also the identity element, the `CertificateVerify` is equally forgeable, completing full TLS impersonation.

**IC-node path is partially mitigated**

`NodeServerCertVerifier`/`NodeClientCertVerifier` at `rs/crypto/src/tls/rustls/node_cert_verifier.rs` lines 238–244 enforce byte-equality with the registry certificate before further validation: [8](#0-7) 

The attacker crafted the registered DER bytes themselves, so they possess them and can present the exact bytes. The `CertificateVerify` for the identity-element public key is still forgeable, so this path is also exploitable, though it requires presenting the exact registered DER.

## Impact Explanation

A registered node operator can register a TLS certificate with the identity-element public key. The boundary node's `TlsVerifier` will then accept any certificate bearing the identity-element public key and a trivially forgeable `(R=B, S=1)` self-signature as authenticating that node. This allows any party to impersonate the registered node in boundary-node TLS connections, enabling forged responses to be served to users routed through the boundary node to that node. This matches the allowed impact: **"Significant boundary/API security impact with concrete user or protocol harm"** — Medium ($200–$2,000).

## Likelihood Explanation

- **Attacker prerequisite**: A valid node operator record, which requires governance approval. This is a meaningful but not extraordinary barrier — node operators are a normal operational role, not a privileged insider class.
- **Craft complexity**: Constructing a DER X.509 certificate with a chosen 32-byte public key and a hand-crafted 64-byte signature is straightforward with any ASN.1 library.
- **No cryptographic hardness**: The forgery requires only `R = [S]B` (pick `S=1`, `R=B`), not discrete-log inversion.
- **Deterministic and locally testable**: The entire path from `add_node` to `verify_server_cert` can be exercised in a unit test without network access.

## Recommendation

Add an `is_canonical()` check inside `verify_ed25519_public_key` in `rs/crypto/node_key_validation/tls_cert_validation/src/lib.rs`:

```rust
fn verify_ed25519_public_key(
    public_key: &ic_ed25519::PublicKey,
) -> Result<(), TlsCertValidationError> {
    if !public_key.is_torsion_free() || !public_key.is_canonical() {
        return Err(invalid_tls_certificate_error(
            "public key verification failed",
        ));
    }
    Ok(())
}
```

This rejects all three non-canonical identity-element encodings at registration time, preventing the malformed certificate from ever reaching the registry.

## Proof of Concept

1. Construct a DER X.509 v3 certificate whose `subjectPublicKey` field is one of the three non-canonical identity-element encodings (e.g., `0x0100000000000000000000000000000000000000000000000000000000000080`).
2. Set the self-signature to `S=1 || R=B` (64 bytes: the Ed25519 base-point compressed followed by the scalar 1 in little-endian).
3. Submit this certificate as `transport_tls_cert` in an `AddNodePayload` via `do_add_node`. Observe that `ValidTlsCertificate::try_from` returns `Ok` and the certificate is written to the registry.
4. In a test harness for `TlsVerifier::verify_server_cert`, load the registry snapshot containing the above certificate, then call `verify_server_cert` with any certificate bearing the identity-element public key and a `(R=B, S=1)` self-signature. Observe that the call returns `Ok(ServerCertVerified::assertion())`.

### Citations

**File:** rs/crypto/node_key_validation/tls_cert_validation/src/lib.rs (L220-229)
```rust
fn verify_ed25519_public_key(
    public_key: &ic_ed25519::PublicKey,
) -> Result<(), TlsCertValidationError> {
    if !public_key.is_torsion_free() {
        return Err(invalid_tls_certificate_error(
            "public key verification failed",
        ));
    }
    Ok(())
}
```

**File:** rs/crypto/node_key_validation/tls_cert_validation/src/lib.rs (L243-258)
```rust
fn verify_ed25519_signature(
    x509_cert: &X509Certificate,
    public_key: &ic_ed25519::PublicKey,
) -> CryptoResult<()> {
    let sig = x509_cert.signature_value.data.as_ref();
    let msg = x509_cert.tbs_certificate.as_ref();

    public_key
        .verify_signature(msg, sig)
        .map_err(|e| CryptoError::SignatureVerification {
            algorithm: AlgorithmId::Ed25519,
            public_key_bytes: public_key.serialize_raw().to_vec(),
            sig_bytes: sig.to_vec(),
            internal_error: e.to_string(),
        })
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

**File:** rs/registry/canister/src/mutations/node_management/common.rs (L179-182)
```rust
    let add_tls_certificate = insert(
        make_crypto_tls_cert_key(node_id).as_bytes(),
        valid_node_pks.tls_certificate().encode_to_vec(),
    );
```

**File:** rs/boundary_node/ic_boundary/src/tls_verify.rs (L107-129)
```rust
        let node_tls_pubkey_from_registry = ic_ed25519::PublicKey::deserialize_raw(
            &node_cert
                .tbs_certificate
                .subject_pki
                .subject_public_key
                .data,
        )
        .map_err(|e| {
            RustlsError::InvalidCertificate(CertificateError::Other(rustls::OtherError(Arc::from(
                Box::from(anyhow!("node cert: invalid Ed25519 public key: {e:?}")),
            ))))
        })?;

        let provided_cert_sig = <[u8; 64]>::try_from(provided_cert.signature_value.data.as_ref())
            .map_err(|e| {
            RustlsError::InvalidCertificate(CertificateError::Other(rustls::OtherError(Arc::from(
                Box::from(anyhow!("node cert: invalid Ed25519 signature: {:?}", e)),
            ))))
        })?;

        node_tls_pubkey_from_registry
            .verify_signature(provided_cert.tbs_certificate.as_ref(), &provided_cert_sig)
            .map_err(|_x| RustlsError::InvalidCertificate(CertificateError::BadSignature))?;
```

**File:** rs/crypto/src/tls/rustls/node_cert_verifier.rs (L238-244)
```rust
    let node_cert_from_registry =
        node_cert_from_registry(end_entity_node_id, registry_client, registry_version)?;
    ensure_certificates_equal(
        end_entity_der.as_ref(),
        end_entity_node_id,
        node_cert_from_registry.as_der(),
    )?;
```
