The full call chain is now clear. Let me confirm the exact behavior of `verify_node_cert` and the absence of any post-TLS authorization check.

**Complete trace:**

1. `start_server` (line 260-263, `xnet/src/lib.rs`) calls `tls.server_config(SomeOrAllNodes::All, ...)`. [1](#0-0) 

2. `server_config` in `server_handshake.rs` constructs a `NodeClientCertVerifier` with `allowed_clients = SomeOrAllNodes::All`. [2](#0-1) 

3. `verify_node_cert` calls `ensure_node_id_in_allowed_nodes`. With `SomeOrAllNodes::All`, `contains()` unconditionally returns `true` for any node ID. [3](#0-2) [4](#0-3) 

4. The only remaining checks are: cert must exist in the registry for that node ID, and must match the presented cert. No subnet-membership check exists. [5](#0-4) 

5. After TLS, `handle_xnet_request` → `route_request` → `handle_stream` serve the certified slice with **no further authorization check**. [6](#0-5) [7](#0-6) 

---

### Title
XNet Endpoint Accepts Any Registered IC Node Due to `SomeOrAllNodes::All`, Allowing Unauthorized Cross-Subnet Stream Data Access — (`rs/http_endpoints/xnet/src/lib.rs`)

### Summary
The XNet HTTPS endpoint uses `SomeOrAllNodes::All` when constructing its TLS server config, meaning any node registered anywhere in the IC registry can complete a mutual-TLS handshake and read certified stream slices, regardless of whether that node belongs to an authorized destination subnet.

### Finding Description
In `start_server` (`rs/http_endpoints/xnet/src/lib.rs`, lines 260–263), the server calls:

```rust
tls.server_config(
    ic_crypto_tls_interfaces::SomeOrAllNodes::All,
    registry_version,
)
```

This passes `SomeOrAllNodes::All` as `allowed_clients`. The resulting `NodeClientCertVerifier` (constructed in `server_handshake.rs` line 30) uses this value in `verify_node_cert` (`node_cert_verifier.rs` line 237):

```rust
ensure_node_id_in_allowed_nodes(end_entity_node_id, allowed_nodes)?;
```

Because `SomeOrAllNodes::All.contains(any_node_id)` always returns `true` (`tls_interfaces/src/lib.rs` line 237), the subnet-membership gate is entirely absent. The only remaining checks are:
- The presented certificate must exist in the registry for the claimed node ID.
- The certificate must match the registry copy byte-for-byte.
- The certificate must be temporally valid.

A node registered on **any** subnet satisfies all three checks using its own legitimate TLS certificate and private key. After the handshake, `handle_xnet_request` → `route_request` → `handle_stream` serve the full `CertifiedStreamSlice` with no further authorization. There is no post-TLS check that the authenticated node belongs to the destination subnet of the requested stream.

### Impact Explanation
An attacker controlling a registered IC node on subnet B can connect to the XNet endpoint of subnet A and request `/api/v1/stream/{subnet_C_id}`, reading inter-canister messages that subnet A is sending to subnet C. XNet stream slices contain plaintext inter-canister message payloads (they are certified for integrity, not encrypted for confidentiality). This violates the invariant that XNet stream access must be restricted to authorized peer subnets.

### Likelihood Explanation
The attacker only needs to be a registered IC node (on any subnet). No threshold corruption, key leakage, or privileged access is required. The attack is fully local-testable: register a node on subnet B, present its valid TLS certificate to subnet A's XNet endpoint, and observe that the handshake succeeds and stream data is returned.

### Recommendation
Replace `SomeOrAllNodes::All` with a `SomeOrAllNodes::Some(allowed_node_set)` that contains only the node IDs belonging to subnets that have active incoming streams from the serving subnet, derived from the registry at the current version. This mirrors how the QUIC transport (`connection_manager.rs` line 210) uses `SomeOrAllNodes::Some(BTreeSet::new())` as a starting point and populates it from topology.

### Proof of Concept
1. Register node `N_attacker` on subnet B; its TLS certificate `C_attacker` is stored in the registry.
2. Connect to the XNet endpoint of subnet A's replica using `C_attacker` and the corresponding private key.
3. In `verify_node_cert`: `ensure_node_id_in_allowed_nodes` passes because `SomeOrAllNodes::All.contains(N_attacker) == true`; `node_cert_from_registry` succeeds because `C_attacker` is in the registry; `ensure_certificates_equal` passes; cert validity passes.
4. TLS handshake completes; `ClientCertVerified::assertion()` is returned.
5. Issue `GET /api/v1/stream/{subnet_C_id}` — `handle_stream` returns the full `CertifiedStreamSlice` for subnet C's stream with HTTP 200.
6. The attacker has read cross-subnet messages intended only for subnet C.

### Citations

**File:** rs/http_endpoints/xnet/src/lib.rs (L152-194)
```rust
async fn handle_xnet_request(
    State(ctx): State<Context<impl CertifiedStreamStore>>,
    request: Request<Body>,
) -> impl IntoResponse {
    let owned_permit = match ctx.semaphore.try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => {
            ctx.metrics
                .request_duration
                .with_label_values(&[RESOURCE_UNKNOWN, StatusCode::SERVICE_UNAVAILABLE.as_str()])
                .observe(0.0);

            return ok(Response::builder()
                .status(StatusCode::SERVICE_UNAVAILABLE)
                .body(Body::from("Queue full"))
                .unwrap());
        }
    };
    let metrics = ctx.metrics.clone();
    let log = ctx.log.clone();
    let certified_stream_store = ctx.certified_stream_store.clone();

    ok(tokio::task::spawn_blocking(move || {
        let _permit = owned_permit;

        match ctx.base_url.join(
            request
                .uri()
                .path_and_query()
                .map(|pq| pq.as_str())
                .unwrap_or(""),
        ) {
            Ok(url) => route_request(url, certified_stream_store.as_ref(), &metrics),
            Err(e) => {
                let msg = format!("Invalid URL {}: {}", request.uri(), e);
                warn!(log, "{}", msg);
                bad_request(msg)
            }
        }
    })
    .await
    .expect("Processing http request panicked!"))
}
```

**File:** rs/http_endpoints/xnet/src/lib.rs (L259-263)
```rust
                            let registry_version = registry_client.get_latest_version();
                            let mut server_config = match tls.server_config(
                                ic_crypto_tls_interfaces::SomeOrAllNodes::All,
                                registry_version,
                            ) {
```

**File:** rs/http_endpoints/xnet/src/lib.rs (L442-475)
```rust
fn handle_stream(
    subnet_id: SubnetId,
    witness_begin: Option<StreamIndex>,
    msg_begin: Option<StreamIndex>,
    msg_limit: Option<usize>,
    byte_limit: Option<usize>,
    certified_stream_store: &impl CertifiedStreamStore,
    metrics: &XNetEndpointMetrics,
) -> Response<Body> {
    let witness_begin = witness_begin.or(msg_begin);
    match certified_stream_store.encode_certified_stream_slice(
        subnet_id,
        witness_begin,
        msg_begin,
        msg_limit,
        byte_limit,
    ) {
        Ok(stream) => {
            metrics
                .slice_payload_size
                .observe(stream.payload.len() as f64);
            observe_response_size(
                || proto_response::<_, pb::CertifiedStreamSlice>(stream),
                RESOURCE_STREAM,
                metrics,
            )
        }
        Err(EncodeStreamError::NoStreamForSubnet(_)) => no_content(),
        Err(e @ EncodeStreamError::InvalidSliceBegin { .. }) => {
            range_not_satisfiable(e.to_string())
        }
        Err(e @ EncodeStreamError::InvalidSliceIndices { .. }) => bad_request(e.to_string()),
    }
}
```

**File:** rs/crypto/src/tls/rustls/server_handshake.rs (L30-34)
```rust
    let client_cert_verifier = NodeClientCertVerifier::new_with_mandatory_client_auth(
        allowed_clients.clone(),
        registry_client,
        registry_version,
    );
```

**File:** rs/crypto/tls_interfaces/src/lib.rs (L234-239)
```rust
    pub fn contains(&self, node_id: NodeId) -> bool {
        match self {
            SomeOrAllNodes::Some(node_ids) => node_ids.contains(&node_id),
            SomeOrAllNodes::All => true,
        }
    }
```

**File:** rs/crypto/src/tls/rustls/node_cert_verifier.rs (L237-237)
```rust
    ensure_node_id_in_allowed_nodes(end_entity_node_id, allowed_nodes)?;
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
