### Title
Missing `is_canonical()` Check Allows Identity-Element TLS Certificate Registration and Potential Node Impersonation — (`rs/crypto/node_key_validation/tls_cert_validation/src/lib.rs`)

---

### Summary

`verify_ed25519_public_key` only calls `is_torsion_free()` and never calls `is_canonical()`. The three non-canonical torsion-free encodings of the Ed25519 identity element pass this check. Under ZIP215 rules (used throughout `ic_ed25519`), the identity element accepts any signature `(R, S)` where `R = [S]B`, so a crafted self-signed certificate with the identity element as the public key also passes `verify_ed25519_signature`. The certificate is then stored in the registry. The boundary node's `TlsVerifier` re-uses `ic_ed25519::PublicKey::verify_signature` (ZIP215) to authenticate peers, so any attacker who presents a certificate with the identity element public key and a forged `(R=B, S=1)` signature passes `verify_server_cert`.

---

### Finding Description

**Step 1 — Public-key validation gap**

`verify_ed25519_public_key` only checks `is_torsion_free()`: [1](#0-0) 

`is_canonical()` exists on `PublicKey` but is never called here: [2](#0-1) 

**Step 2 — The three non-canonical identity encodings are torsion-free**

The test suite explicitly documents and confirms this: [3](#0-2) 

All three encodings satisfy `is_torsion_free() == true` and `is_canonical() == false`.

**Step 3 — Self-signature passes under ZIP215**

`verify_ed25519_signature` calls `ic_ed25519::PublicKey::verify_signature`, which implements ZIP215: [4](#0-3) 

The ZIP215 verification equation is `(recomputed_r − R).mul_by_cofactor().is_identity()`, where `recomputed_r = [S]B` when `A = identity`: [5](#0-4) 

With `A = identity`, `[k]A = identity`, so the check reduces to `([S]B − R).mul_by_cofactor() == identity`. Choosing `S = 1`, `R = B` satisfies this for **any** message `M`. The attacker can therefore produce a valid self-signature over any TBSCertificate.

**Step 4 — Certificate reaches the registry**

`do_add_node` calls `valid_keys_from_payload` → `ValidNodePublicKeys::try_from` → `ValidTlsCertificate::try_from`: [6](#0-5) 

If `ValidTlsCertificate::try_from` returns `Ok`, the certificate is inserted into the registry: [7](#0-6) 

**Step 5 — Boundary node `TlsVerifier` uses ZIP215 for peer authentication**

`TlsVerifier::verify_server_cert` extracts the public key from the **registry** certificate and calls `ic_ed25519::PublicKey::verify_signature` (ZIP215) on the **presented** certificate: [8](#0-7) 

If the registry certificate carries the identity element, any presented certificate with the identity element public key and a forged `(R=B, S=1)` signature passes this check.

---

### Impact Explanation

A registered node operator can submit an `add_node` payload whose `transport_tls_cert` embeds one of the three non-canonical torsion-free identity-element encodings. The certificate passes all validation gates and is written to the registry. Subsequently:

- The boundary node's `TlsVerifier` accepts any certificate bearing the identity element public key and a trivially forgeable signature, allowing an attacker to impersonate the registered node in P2P/boundary TLS connections.
- The IC's `NodeServerCertVerifier` / `NodeClientCertVerifier` additionally enforce byte-equality with the registry certificate (`ensure_certificates_equal`), which limits impersonation to parties who possess the exact registered DER bytes — but the attacker crafted those bytes themselves, so they have them. [9](#0-8) 

---

### Likelihood Explanation

- **Attacker prerequisite**: a valid node operator record (requires governance approval), but this is a normal operational role, not a privileged insider.
- **Craft complexity**: constructing a DER X.509 certificate with a chosen 32-byte public key and a hand-crafted 64-byte signature is straightforward with any ASN.1 library.
- **No cryptographic hardness**: the forgery requires only `R = [S]B` (pick `S=1`, `R=B`), not discrete-log inversion.
- **Deterministic, locally testable**: the entire path from `add_node` to `verify_server_cert` can be exercised in a unit test without network access.

---

### Recommendation

Add an `is_canonical()` check inside `verify_ed25519_public_key`:

```rust
fn verify_ed25519_public_key(
    public_key: &ic_ed25519::PublicKey,
) -> Result<(), TlsCertValidationError> {
    if !public_key.is_torsion_free() || !public_key.is_canonical

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

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L339-399)
```rust
/// Validates the payload and extracts node's public keys
fn valid_keys_from_payload(
    payload: &AddNodePayload,
) -> Result<(NodeId, ValidNodePublicKeys), String> {
    // 1. verify that the keys we got are not empty
    if payload.node_signing_pk.is_empty() {
        return Err(String::from("node_signing_pk is empty"));
    };
    if payload.committee_signing_pk.is_empty() {
        return Err(String::from("committee_signing_pk is empty"));
    };
    if payload.ni_dkg_dealing_encryption_pk.is_empty() {
        return Err(String::from("ni_dkg_dealing_encryption_pk is empty"));
    };
    if payload.transport_tls_cert.is_empty() {
        return Err(String::from("transport_tls_cert is empty"));
    };

    // 2. get the keys for verification -- for that, we need to create
    // NodePublicKeys first
    let node_signing_pk = PublicKey::decode(&payload.node_signing_pk[..])
        .map_err(|e| format!("node_signing_pk is not in the expected format: {e:?}"))?;
    let committee_signing_pk = PublicKey::decode(&payload.committee_signing_pk[..])
        .map_err(|e| format!("committee_signing_pk is not in the expected format: {e:?}"))?;
    let tls_certificate = X509PublicKeyCert::decode(&payload.transport_tls_cert[..])
        .map_err(|e| format!("transport_tls_cert is not in the expected format: {e:?}"))?;
    let dkg_dealing_encryption_pk = PublicKey::decode(&payload.ni_dkg_dealing_encryption_pk[..])
        .map_err(|e| {
            format!("ni_dkg_dealing_encryption_pk is not in the expected format: {e:?}")
        })?;

    // TODO(NNS1-1197): Refactor when nodes are provisioned for threshold ECDSA subnets
    let idkg_dealing_encryption_pk = match &payload.idkg_dealing_encryption_pk {
        None => return Err(String::from("idkg_dealing_encryption_pk is missing")),
        Some(pk) if pk.is_empty() => {
            return Err(String::from("idkg_dealing_encryption_pk is empty"));
        }

        Some(pk) => PublicKey::decode(&pk[..]).map_err(|e| {
            format!("idkg_dealing_encryption_pk is not in the expected format: {e:?}")
        })?,
    };

    // 3. get the node id from the node_signing_pk
    let node_id = crypto_basicsig_conversions::derive_node_id(&node_signing_pk)
        .map_err(|e| format!("node signing public key couldn't be converted to a NodeId: {e:?}"))?;

    // 4. get the keys for verification -- for that, we need to create
    let node_pks = CurrentNodePublicKeys {
        node_signing_public_key: Some(node_signing_pk),
        committee_signing_public_key: Some(committee_signing_pk),
        tls_certificate: Some(tls_certificate),
        dkg_dealing_encryption_public_key: Some(dkg_dealing_encryption_pk),
        idkg_dealing_encryption_public_key: Some(idkg_dealing_encryption_pk),
    };

    // 5. validate the keys and the node_id
    match ValidNodePublicKeys::try_from(node_pks, node_id, now()?) {
        Ok(valid_pks) => Ok((node_id, valid_pks)),
        Err(e) => Err(format!("Could not validate public keys, due to {e:?}")),
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

**File:** rs/crypto/src/tls/rustls/node_cert_verifier.rs (L238-252)
```rust
    let node_cert_from_registry =
        node_cert_from_registry(end_entity_node_id, registry_client, registry_version)?;
    ensure_certificates_equal(
        end_entity_der.as_ref(),
        end_entity_node_id,
        node_cert_from_registry.as_der(),
    )?;
    // It's important to do the validity check after checking equality to the
    // registry cert because the cert validation uses a different parser
    // (`x509_parser` as opposed to OpenSSL that is used above) and it is safer
    // to not just pass any untrusted data to it. We consider the DER here trusted
    // because it is equal to the certificate DER stored in the registry, as checked
    // above.
    ensure_node_certificate_is_valid(end_entity_der.to_vec(), end_entity_node_id, current_time)?;
    Ok(())
```
