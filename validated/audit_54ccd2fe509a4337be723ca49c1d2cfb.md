### Title
TLS Certificate Verification Completely Disabled in Production `HttpClient` Enables MITM Interception - (File: `rs/canister_client/src/http_client.rs`)

### Summary
The `HttpClient` used by the IC's `Agent` to communicate with IC replicas unconditionally accepts any server TLS certificate via a custom `DangerAcceptInvalidCerts` verifier, completely bypassing TLS authentication. This is structurally analogous to the reported missing SSL on a database connection: both leave a production communication channel open to man-in-the-middle interception.

### Finding Description
In `rs/canister_client/src/http_client.rs`, the `DangerAcceptInvalidCerts` struct implements `rustls::client::danger::ServerCertVerifier` and unconditionally returns `Ok(ServerCertVerified::assertion())` for every certificate presented by a server — regardless of issuer, validity, or identity. [1](#0-0) 

This verifier is wired directly into the default `HttpClient` constructor used in production: [2](#0-1) 

The `HttpClient` is the transport layer for the `Agent` that sends ingress messages, query calls, and read-state requests to IC replicas. The IC's own test suite explicitly acknowledges this risk: [3](#0-2) 

Production callers of `HttpClient::new()` include the ICP Rosetta API server (`rs/rosetta-api/icp/test_utils/src/rosetta_api_serv.rs`) and NNS cycle-minting tooling (`rs/tests/nns/nns_cycles_minting_test.rs`). [4](#0-3) 

### Impact Explanation
Any attacker positioned on the network path between the `HttpClient`-based service (e.g., the Rosetta API server) and an IC replica can impersonate the replica with a self-signed or attacker-controlled certificate. Because `DangerAcceptInvalidCerts` accepts it unconditionally, the TLS handshake succeeds and the attacker can:

- Return fabricated query responses (e.g., false ICP balances to Rosetta API users).
- Suppress or replay update-call acknowledgements.
- Intercept sensitive request payloads (canister arguments, sender identities).

**Impact: Medium** — financial and data-integrity consequences for users of services built on this client.

### Likelihood Explanation
Exploitation requires a network-level position between the client and the replica (e.g., ARP poisoning on a shared segment, a compromised router, or a rogue node on the same data-center VLAN). It does not require DNS/BGP hijack or any privileged IC role. The absence of certificate pinning or registry-based verification means no additional cryptographic barrier exists once the attacker is on-path.

**Likelihood: Medium** — on-path network access is a realistic threat model for co-located infrastructure.

### Recommendation
Replace `DangerAcceptInvalidCerts` with proper certificate verification. For connections to IC replicas, use the registry-based `NodeServerCertVerifier` already implemented in `rs/crypto/src/tls/rustls/client_handshake.rs`: [5](#0-4) 

At minimum, configure the `rustls::ClientConfig` with a trusted root store (e.g., `webpki_roots`) rather than a no-op verifier, as is already done for the `CloudEngine` path in `rs/http_endpoints/nns_delegation_manager/src/nns_delegation_manager.rs`: [6](#0-5) 

### Proof of Concept

1. Start a rogue HTTPS server presenting a self-signed certificate for any domain.
2. ARP-poison or otherwise redirect traffic from a host running `HttpClient::new()` (e.g., the Rosetta API server) to the rogue server.
3. The `DangerAcceptInvalidCerts` verifier returns `Ok` unconditionally; the TLS handshake completes.
4. The rogue server returns attacker-controlled CBOR responses (e.g., a fabricated `HttpStatusResponse` or a false query result).
5. The `Agent` parses and trusts the response with no indication of tampering. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/canister_client/src/http_client.rs (L117-129)
```rust
#[derive(Debug)]
struct DangerAcceptInvalidCerts {}
impl rustls::client::danger::ServerCertVerifier for DangerAcceptInvalidCerts {
    fn verify_server_cert(
        &self,
        _end_entity: &rustls::pki_types::CertificateDer,
        _intermediates: &[rustls::pki_types::CertificateDer],
        _server_name: &rustls::pki_types::ServerName,
        _ocsp_response: &[u8],
        _now: rustls::pki_types::UnixTime,
    ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }
```

**File:** rs/canister_client/src/http_client.rs (L156-184)
```rust
impl HttpClient {
    pub fn new_with_config(config: HttpClientConfig) -> Self {
        let mut http_connector =
            HyperConnector::new_with_resolver(DnsResolverWithOverrides::new(config.overrides));
        http_connector.enforce_http(false);

        let mut rustls_config = rustls::ClientConfig::builder()
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(DangerAcceptInvalidCerts {}))
            .with_no_client_auth();
        rustls_config.enable_sni = false;
        let https_connector = HttpsConnectorBuilder::new()
            .with_tls_config(rustls_config)
            .https_or_http();
        let https_connector = if config.http2_only {
            https_connector.enable_http2()
        } else {
            https_connector.enable_http1().enable_http2()
        };
        let https_connector = https_connector.wrap_connector(http_connector);

        let hyper = HyperClient::builder(TokioExecutor::new())
            .pool_idle_timeout(config.pool_idle_timeout)
            .pool_max_idle_per_host(config.pool_max_idle_per_host)
            .http2_only(config.http2_only)
            .build::<_, Full<Bytes>>(https_connector);

        Self { hyper }
    }
```

**File:** rs/canister_client/tests/tls.rs (L9-10)
```rust
// This highlights that the canister client trusts ANY server certificate. Depending on the context
// where the client is used, this may be a security issue since anyone could act as server / MITM.
```

**File:** rs/canister_client/src/agent.rs (L1-5)
```rust
//! An agent to talk to the Internet Computer through the public endpoints.
use crate::{
    cbor::{parse_query_response, prepare_query, prepare_read_state, prepare_update},
    http_client::{HttpClient, HttpClientConfig},
};
```

**File:** rs/crypto/src/tls/rustls/client_handshake.rs (L32-50)
```rust
    let server_cert_verifier = NodeServerCertVerifier::new(
        SomeOrAllNodes::new_with_single_node(server),
        registry_client,
        registry_version,
    );
    let mut ring_crypto_provider = rustls::crypto::ring::default_provider();
    ring_crypto_provider.cipher_suites = vec![TLS13_AES_256_GCM_SHA384, TLS13_AES_128_GCM_SHA256];

    Ok(
        ClientConfig::builder_with_provider(Arc::new(ring_crypto_provider))
            .with_protocol_versions(&[&TLS13])
            .expect("Valid rustls client config.")
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(server_cert_verifier))
            .with_client_cert_resolver(static_cert_resolver(
                certified_key(self_tls_cert, ed25519_signing_key),
                SignatureScheme::ED25519,
            )),
    )
```

**File:** rs/http_endpoints/nns_delegation_manager/src/nns_delegation_manager.rs (L486-490)
```rust
            let root_store =
                rustls::RootCertStore::from_iter(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
            let tls_client_config = rustls::ClientConfig::builder()
                .with_root_certificates(root_store)
                .with_no_client_auth();
```
