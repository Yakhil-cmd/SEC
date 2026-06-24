Looking at the actual code in `rs/boundary_node/ic_boundary/src/tls_verify.rs`, the verification logic is:

The code is clear. Let me compare the two verifier implementations directly.

**Boundary node `TlsVerifier`** (`rs/boundary_node/ic_boundary/src/tls_verify.rs`, lines 107–129):

```rust
let node_tls_pubkey_from_registry = ic_ed25519::PublicKey::deserialize_raw(
    &node_cert.tbs_certificate.subject_pki.subject_public_key.data,
)...;

let provided_cert_sig = <[u8; 64]>::try_from(provided_cert.signature_value.data.as_ref())...;

node_tls_pubkey_from_registry
    .verify_signature(provided_cert.tbs_certificate.as_ref(), &provided_cert_sig)
    .map_err(|_x| RustlsError::InvalidCertificate(CertificateError::BadSignature))?;
```

**Crypto stack `NodeClientCertVerifier`** (`rs/crypto/src/tls/rustls/node_cert_verifier.rs`, lines 240–244):

```rust
ensure_certificates_equal(
    end_entity_der.as_ref(),
    end_entity_node_id,
    node_cert_from_registry.as_der(),
)?;
```

The gap is unambiguous. The boundary node verifier only checks that the TBS of the presented cert was signed by the registry public key. It never checks that the presented cert's SPKI equals the registry cert's SPKI, nor does it do full DER byte equality. The crypto stack's verifier does full byte equality via `ensure_certificates_equal` and explicitly rejects any cert that "differs from the registry certificate."

---

### Title
Missing SubjectPublicKeyInfo equality check in boundary node `TlsVerifier::verify_server_cert` allows node to use unregistered TLS key pair — (`rs/boundary_node/ic_boundary/src/tls_verify.rs`)

### Summary

The boundary node's custom `TlsVerifier` only verifies that the presented certificate's TBS was signed by the registry public key. It does not verify that the presented certificate's `SubjectPublicKeyInfo` (SPKI) matches the registry certificate's SPKI, nor does it perform full DER byte equality. A node operator who possesses their registered Ed25519 private key can craft a certificate containing an arbitrary new public key, sign its TBS with the registered private key, and present it to the boundary node. The boundary node will accept it, and the TLS session will be established using the unregistered key pair.

### Finding Description

In `verify_server_cert`:

1. The registry certificate is parsed and its raw public key bytes are extracted from `node_cert.tbs_certificate.subject_pki.subject_public_key.data`. [1](#0-0) 

2. The signature from the **presented** certificate is extracted and verified against the **presented** certificate's TBS using the registry public key. [2](#0-1) 

3. There is no comparison between `provided_cert.tbs_certificate.subject_pki` and `node_cert.tbs_certificate.subject_pki`, and no byte-equality check between `end_entity` and `node.tls_certificate`. [3](#0-2) 

The crypto stack's `NodeClientCertVerifier` / `NodeServerCertVerifier` correctly enforces full DER byte equality via `ensure_certificates_equal`, which rejects any cert that differs from the registry cert even if it is signed with the same key. [4](#0-3) 

The `TlsConfig` interface documentation explicitly states the intended invariant: "C_registry is equal to C_handshake." [5](#0-4) 

### Impact Explanation

A node operator with access to their registered TLS private key can:
1. Generate a fresh key pair `(new_pub, new_priv)`.
2. Construct a TBS with `CN=node_id`, `SPKI=new_pub`.
3. Sign the TBS with `registry_priv` to produce a valid signature.
4. Present the crafted cert to the boundary node.
5. `verify_server_cert` returns `ServerCertVerified::assertion()` because the signature check passes.
6. The TLS handshake completes using `new_pub`/`new_priv` (Rustls's `verify_tls13_signature` uses the cert's embedded public key to verify the handshake transcript).

The boundary node now believes it has an authenticated session with the registered node, but the session key material is entirely outside the registry's key management. This bypasses key rotation enforcement, allows use of keys not subject to IC governance, and breaks the invariant that boundary-to-replica TLS sessions use only registry-attested keys.

### Likelihood Explanation

The precondition is possession of the node's registered TLS private key, which the node operator holds. No external compromise, threshold attack, or privileged governance action is required. The attack is fully local to the node operator and requires only standard X.509 certificate construction. The boundary node's `TlsVerifier` is the only verifier on this path; the stronger crypto-stack verifier is not used here.

### Recommendation

Replace the signature-only check with full DER byte equality, matching the pattern already used in the crypto stack:

```rust
if end_entity.as_ref() != node.tls_certificate.as_slice() {
    return Err(RustlsError::General(format!(
        "The peer certificate differs from the registry certificate for node {node_id}"
    )));
}
```

This is exactly what `ensure_certificates_equal` does in `rs/crypto/src/tls/rustls/node_cert_verifier.rs`. [4](#0-3) 

### Proof of Concept

```rust
// 1. Generate registry key pair (simulating the node's registered key)
let registry_key_pair = Ed25519KeyPair::generate();
let registry_pub = registry_key_pair.public_key();

// 2. Generate a new, unregistered key pair
let new_key_pair = Ed25519KeyPair::generate();
let new_pub = new_key_pair.public_key();

// 3. Build a TBS with CN=node_id, SPKI=new_pub
let tbs = build_tbs(node_id, new_pub, validity);

// 4. Sign TBS with registry_priv
let sig = registry_key_pair.sign(&tbs);

// 5. Assemble crafted cert = (tbs, sig)
let crafted_cert = assemble_cert(tbs, sig);

// 6. Feed to TlsVerifier::verify_server_cert
// registry snapshot contains registry_pub for node_id
let result = tls_verifier.verify_server_cert(&crafted_cert, &[], &server_name, &[], now);

// Expected (current behavior): Ok(ServerCertVerified)  ← VULNERABILITY
// Expected (correct behavior): Err(BadSignature or General)
assert!(result.is_ok()); // passes against current code
```

The TLS handshake then proceeds using `new_pub`/`new_priv`, completing successfully because Rustls's `verify_tls13_signature` uses the SPKI embedded in the accepted certificate. [6](#0-5)

### Citations

**File:** rs/boundary_node/ic_boundary/src/tls_verify.rs (L95-139)
```rust
        // Cert is parsed & checked when we read it from the registry - it should be correct.
        // Storing X509Certificate directly in Node is problematic since it does not own the data,
        // it's a zero-copy view over byte array.
        let (_, node_cert) = X509Certificate::from_der(&node.tls_certificate).map_err(|e| {
            RustlsError::General(format!("unable to parse Node TLS certificate: {e:#}"))
        })?;

        // Parse the certificate provided by server
        let (_, provided_cert) = X509Certificate::from_der(end_entity)
            .map_err(|_x| RustlsError::InvalidCertificate(CertificateError::BadEncoding))?;

        // Verify the provided self-signed certificate using the public key from registry
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

        // Check if the certificate is valid at provided `now` time
        if !provided_cert
            .validity
            .is_valid_at(ASN1Time::from_timestamp(now.as_secs() as i64).unwrap())
        {
            return Err(RustlsError::InvalidCertificate(CertificateError::Expired));
        }

        Ok(ServerCertVerified::assertion())
```

**File:** rs/boundary_node/ic_boundary/src/tls_verify.rs (L156-168)
```rust
    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(
            message,
            cert,
            dss,
            &rustls::crypto::ring::default_provider().signature_verification_algorithms,
        )
    }
```

**File:** rs/crypto/src/tls/rustls/node_cert_verifier.rs (L289-299)
```rust
fn ensure_certificates_equal(
    end_entity_cert: &[u8],
    node_id: NodeId,
    node_cert_from_registry: &Vec<u8>,
) -> Result<(), TLSError> {
    if node_cert_from_registry != end_entity_cert {
        return Err(TLSError::General(format!(
            "The peer certificate is not trusted since it differs from the registry certificate. NodeId of presented cert: {node_id}"
        )));
    }
    Ok(())
```

**File:** rs/crypto/tls_interfaces/src/lib.rs (L114-117)
```rust
    /// 2. Determine the certificate C_registry by querying the registry for the
    ///    TLS certificate of node with ID N_claimed, and if C_registry is equal
    ///    to C_handshake, then the peer successfully authenticated as node
    ///    N_claimed.
```
