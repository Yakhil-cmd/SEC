### Title
Missing URL Scheme Validation at Execution Layer Enables Unencrypted Canister HTTP Outcalls When `http` Feature Is Active - (File: `rs/https_outcalls/adapter/src/rpc_server.rs`)

---

### Summary

The Internet Computer's canister HTTP outcalls feature accepts `http://` (unencrypted) URLs at the execution environment layer without any scheme validation. The only HTTPS enforcement exists at the adapter layer (`rs/https_outcalls/adapter/src/rpc_server.rs`) and is gated behind a compile-time feature flag `#[cfg(not(feature = "http"))]`. When the `http` feature is compiled in, the adapter allows `http://` URLs to any external host — not just localhost — enabling a man-in-the-middle attack on canister HTTP outcalls.

---

### Finding Description

The `generate_from_args` and `generate_from_flexible_args` functions in `rs/types/types/src/canister_http.rs` validate URL length, headers, body size, and transform principal — but perform **no URL scheme validation**: [1](#0-0) 

The URL is accepted as-is and stored in the `CanisterHttpRequestContext`. The only scheme enforcement exists in the adapter: [2](#0-1) 

This check is wrapped in `#[cfg(not(feature = "http"))]`. When the `http` feature is compiled in, the entire HTTPS enforcement block is omitted. The test `test_canister_http_http_protocol_allowed` confirms that with the `http` feature, `http://` URLs succeed: [3](#0-2) 

Critically, the test uses `127.0.0.1` (localhost), but the adapter code itself imposes **no host restriction** when the `http` feature is active — any external `http://` URL is permitted.

---

### Impact Explanation

When the `http` feature is compiled into the adapter binary (e.g., accidentally in a production build, or in a staging/testnet environment), any canister can issue HTTP outcalls to arbitrary external hosts over unencrypted `http://`. An attacker on the network path between the IC replica nodes and the target server can intercept and modify the HTTP response. Since canister logic may act on the returned data (e.g., price feeds, oracle data, cross-chain state), a tampered response can corrupt canister state or trigger incorrect on-chain actions. The execution environment never warns or rejects the `http://` URL — it is silently accepted and queued.

---

### Likelihood Explanation

The `http` feature is a compile-time flag intended for testing. In mainnet production builds it is not enabled, so the adapter enforces HTTPS. However:

1. The execution environment layer has **zero scheme validation** — the entire security property rests on a single conditional block in the adapter.
2. Any testnet, staging, or developer-operated subnet that compiles with `--features http` is fully exposed.
3. The missing validation at the execution layer means there is no defense-in-depth: if the adapter check is ever removed, misconfigured, or bypassed, `http://` URLs flow through silently.

Likelihood is **medium** for non-mainnet deployments and **low** for mainnet, but the architectural gap (no scheme check at the execution layer) is a persistent structural weakness.

---

### Recommendation

Add URL scheme validation inside `generate_from_args` and `generate_from_flexible_args` in `rs/types/types/src/canister_http.rs`, rejecting any URL that does not begin with `https://` (mirroring the existing `validate_url_length` pattern). This enforces the invariant at the protocol layer regardless of adapter build configuration, providing defense-in-depth. The adapter-level check can remain as a secondary guard. [4](#0-3) 

A new `validate_url_scheme` function should be added alongside `validate_url_length` and called from both `generate_from_args` and `generate_from_flexible_args`.

---

### Proof of Concept

1. A canister calls `ic00::http_request` with `url: "http://attacker-controlled-host.example.com/data"`.
2. `generate_from_args` is invoked; it calls `validate_url_length` (passes) and `validate_http_headers_and_body` (passes) — no scheme check occurs. [5](#0-4) 
3. The `CanisterHttpRequestContext` is stored with the `http://` URL intact.
4. The adapter receives the request. In a build with `--features http`, the `#[cfg(not(feature = "http"))]` block is compiled out, so no scheme rejection occurs. [2](#0-1) 
5. The adapter issues an unencrypted HTTP request to the external host. An attacker on the network path performs a MitM, returning a crafted response that the canister processes as legitimate external data.

### Citations

**File:** rs/types/types/src/canister_http.rs (L486-492)
```rust
fn validate_url_length(url: &str) -> Result<(), CanisterHttpRequestContextError> {
    let url_len = url.len();
    if url_len > MAX_CANISTER_HTTP_URL_SIZE {
        return Err(CanisterHttpRequestContextError::UrlTooLong(url_len));
    }
    Ok(())
}
```

**File:** rs/types/types/src/canister_http.rs (L510-519)
```rust
    pub fn generate_from_args(
        time: Time,
        request: &Request,
        args: CanisterHttpRequestArgs,
        node_ids: &BTreeSet<NodeId>,
        rng: &mut dyn RngCore,
    ) -> Result<Self, CanisterHttpRequestContextError> {
        validate_transform_principal(&args.transform, request.sender.get())?;
        validate_url_length(&args.url)?;
        validate_http_headers_and_body(args.headers.get(), args.body.as_ref().unwrap_or(&vec![]))?;
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L267-282)
```rust
            #[cfg(not(feature = "http"))]
            if uri.scheme() != Some(&http::uri::Scheme::HTTPS) {
                use crate::metrics::LABEL_HTTP_SCHEME;
                debug!(
                    self.logger,
                    "Got request with no or http scheme specified. {}", uri
                );
                self.metrics
                    .request_errors
                    .with_label_values(&[LABEL_HTTP_SCHEME])
                    .inc();
                return Err(CanisterHttpError {
                    kind: CanisterHttpErrorKind::InvalidInput as i32,
                    message: "Url need to specify https scheme".to_string(),
                });
            }
```

**File:** rs/https_outcalls/adapter/tests/server_test.rs (L474-498)
```rust
    #[cfg(feature = "http")]
    #[tokio::test]
    async fn test_canister_http_http_protocol_allowed() {
        // Check that error is returned if a `http` url is specified.
        let path = "/tmp/canister-http-test-".to_string() + &Uuid::new_v4().to_string();
        let server_config = Config {
            incoming_source: IncomingSource::Path(path.into()),
            ..Default::default()
        };

        let url = start_http_server("127.0.0.1".parse().unwrap());
        let mut client = spawn_grpc_server(server_config);

        let request = tonic::Request::new(HttpsOutcallRequest {
            url: format!("http://{}/get", &url),
            headers: Vec::new(),
            method: HttpMethod::Get as i32,
            body: "hello".to_string().as_bytes().to_vec(),
            max_response_size_bytes: 512,
            ..Default::default()
        });
        let response = client.https_outcall(request).await.unwrap();
        let (http_response, _) = unwrap_response(response);
        assert_eq!(http_response.status, StatusCode::OK.as_u16() as u32);
    }
```
