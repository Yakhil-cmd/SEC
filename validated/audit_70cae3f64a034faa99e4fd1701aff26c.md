### Title
TLS Certificate Verification Disabled by Default in Disaster Recovery API Boundary Node - (File: `ic-os/api-bn-recovery/entrypoint.sh`)

---

### Summary

The disaster recovery API boundary node container (`ic-os/api-bn-recovery/`) ships with `SKIP_REPLICA_TLS_VERIFICATION` defaulting to `true`, which causes `ic-boundary` to use a no-op TLS verifier (`NoopServerCertVerifier`) for all connections to IC replica nodes. This disables the registry-backed certificate pinning that normally authenticates replicas, exposing the boundary node to man-in-the-middle attacks on the boundary-to-replica TLS channel.

---

### Finding Description

In `ic-os/api-bn-recovery/entrypoint.sh`, line 22 sets:

```bash
SKIP_REPLICA_TLS_VERIFICATION="${SKIP_REPLICA_TLS_VERIFICATION:-true}"
```

This default is passed directly to `ic-boundary` via `--skip-replica-tls-verification` at line 124:

```bash
if [ "${SKIP_REPLICA_TLS_VERIFICATION}" = "true" ]; then
    BOUNDARY_ARGS+=(--skip-replica-tls-verification)
fi
```

In `rs/boundary_node/ic_boundary/src/core.rs`, lines 161–165, when this flag is set, the production `TlsVerifier` (which pins replica certificates against the IC registry) is replaced with `NoopServerCertVerifier`:

```rust
let tls_verifier: Arc<dyn ServerCertVerifier> = if cli.misc.skip_replica_tls_verification {
    Arc::new(NoopServerCertVerifier::default())
} else {
    Arc::new(TlsVerifier::new(registry_snapshot.clone()))
};
```

The `TlsVerifier` in `rs/boundary_node/ic_boundary/src/tls_verify.rs` performs registry-backed certificate pinning: it extracts the node ID from the certificate CN, looks up the node's public key in the registry snapshot, and cryptographically verifies the self-signed certificate. With `NoopServerCertVerifier`, `verify_server_cert` unconditionally returns `Ok(ServerCertVerified::assertion())` — no checks are performed.

The CLI flag is documented as "DANGER: to be used only for testing" in `rs/boundary_node/ic_boundary/src/cli.rs` line 454, yet the disaster recovery container defaults it to `true` for any operator who does not explicitly override the environment variable.

---

### Impact Explanation

An attacker with a network-level position between the disaster recovery boundary node and any IC replica node (e.g., on the same data-center network, or via BGP/routing manipulation targeting the specific host) can impersonate a replica node. Because TLS certificate verification is entirely skipped, the attacker can:

- Intercept and read unencrypted (post-TLS-termination) query request/response payloads forwarded by the boundary node.
- Return fabricated query responses to users. Non-certified query responses carry no subnet signature, so users cannot detect forgery at the application layer.
- Selectively drop or delay ingress call messages, causing silent failures for users submitting transactions through the recovery boundary node.

The disaster recovery boundary node is specifically intended for use during NNS outages, when it is the **only** path for the community to reach the NNS and vote on recovery proposals. Compromising this path during a critical incident has outsized protocol-level impact.

---

### Likelihood Explanation

The default is `true` in the shipped container entrypoint. Any operator following the documented deployment steps (setting only `TLS_HOSTNAME` or `TLS_CERT_PATH`/`TLS_PKEY_PATH`) will deploy with TLS verification disabled unless they explicitly add `SKIP_REPLICA_TLS_VERIFICATION=false` to their `docker run` invocation — a step not mentioned in the README. The README (`ic-os/api-bn-recovery/README.md`) shows no mention of this variable or its security implications. The attacker entry path requires only a network-adjacent position, not any IC-level privilege.

---

### Recommendation

Change the default in `ic-os/api-bn-recovery/entrypoint.sh` from `true` to `false`:

```bash
SKIP_REPLICA_TLS_VERIFICATION="${SKIP_REPLICA_TLS_VERIFICATION:-false}"
```

Additionally, document the variable and its security implications in the README, and consider removing the `--skip-replica-tls-verification` flag from production binaries entirely, or gating it behind a compile-time feature flag.

---

### Proof of Concept

1. Deploy the container per the README with `TLS_HOSTNAME` set (standard HTTPS production mode). No `SKIP_REPLICA_TLS_VERIFICATION` override is provided.
2. Observe that `ic-boundary` is launched with `--skip-replica-tls-verification` (line 124 of `entrypoint.sh`).
3. In `core.rs` lines 161–162, `NoopServerCertVerifier` is installed as the TLS verifier for all replica connections.
4. Position a TLS-intercepting proxy (e.g., `mitmproxy`) on the network path between the boundary node host and any replica IP in the registry.
5. The boundary node completes the TLS handshake with the proxy without error, forwarding user query traffic through the attacker-controlled channel.
6. The attacker returns a crafted query response; the boundary node forwards it to the user without any certificate or signature check on the transport layer.

---

**Root cause references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** ic-os/api-bn-recovery/entrypoint.sh (L22-22)
```shellscript
SKIP_REPLICA_TLS_VERIFICATION="${SKIP_REPLICA_TLS_VERIFICATION:-true}"
```

**File:** ic-os/api-bn-recovery/entrypoint.sh (L123-125)
```shellscript
if [ "${SKIP_REPLICA_TLS_VERIFICATION}" = "true" ]; then
    BOUNDARY_ARGS+=(--skip-replica-tls-verification)
fi
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L160-165)
```rust
    // Pick a TLS certificate verifier - Registry-based or a No-op one
    let tls_verifier: Arc<dyn ServerCertVerifier> = if cli.misc.skip_replica_tls_verification {
        Arc::new(NoopServerCertVerifier::default())
    } else {
        Arc::new(TlsVerifier::new(registry_snapshot.clone()))
    };
```

**File:** rs/boundary_node/ic_boundary/src/cli.rs (L454-456)
```rust
    /// Skip replica TLS certificate verification. DANGER: to be used only for testing
    #[clap(env, long)]
    pub skip_replica_tls_verification: bool,
```

**File:** rs/boundary_node/ic_boundary/src/tls_verify.rs (L28-55)
```rust
impl ServerCertVerifier for TlsVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        now: UnixTime,
    ) -> Result<ServerCertVerified, RustlsError> {
        if !intermediates.is_empty() {
            return Err(RustlsError::General(format!(
                "The peer must send exactly one self signed certificate, but it sent {} certificates.",
                intermediates.len() + 1
            )));
        }

        // Check if the CommonName in the certificate can be parsed into a Principal
        let node_id =
            node_id_from_certificate_der(end_entity.as_ref()).map_err(|err| match err {
                NodeIdFromCertificateDerError::InvalidCertificate(_) => {
                    RustlsError::InvalidCertificate(CertificateError::BadEncoding)
                }
                NodeIdFromCertificateDerError::UnexpectedContent(e) => {
                    RustlsError::InvalidCertificate(CertificateError::Other(rustls::OtherError(
                        Arc::from(Box::from(anyhow!("unexpected certificate content: {e:#}"))),
                    )))
                }
            })?;
```
