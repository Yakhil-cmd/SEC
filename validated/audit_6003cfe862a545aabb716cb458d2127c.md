### Title
Missing URL Scheme Validation in Canister HTTP Request Context Generation Allows HTTP Plaintext Outcalls - (File: rs/types/types/src/canister_http.rs)

### Summary
The `CanisterHttpRequestContext::generate_from_args` and `generate_from_flexible_args` functions in the execution environment validate canister HTTP outcall URLs only for length, not for scheme. The HTTPS enforcement is deferred entirely to the adapter layer and is gated behind a compile-time feature flag (`#[cfg(not(feature = "http"))]`). When the `http` feature is compiled in, the scheme check is completely absent, allowing canisters to make plaintext HTTP outcalls. This is structurally analogous to the reported bug where only the hostname (not the full origin including protocol) was used for permission checks.

### Finding Description

`CanisterHttpRequestContext::generate_from_args` performs three validations before accepting a canister HTTP request into replicated state: [1](#0-0) 

```rust
validate_transform_principal(&args.transform, request.sender.get())?;
validate_url_length(&args.url)?;
validate_http_headers_and_body(...)?;
```

`validate_url_length` only checks the byte length of the URL string: [2](#0-1) 

There is no scheme validation. A canister can submit `http://`, `ftp://`, or any other scheme and the execution environment will accept it, store it in replicated state, and charge cycles.

The HTTPS enforcement only exists in the adapter: [3](#0-2) 

```rust
#[cfg(not(feature = "http"))]
if uri.scheme() != Some(&http::uri::Scheme::HTTPS) {
    return Err(CanisterHttpError {
        kind: CanisterHttpErrorKind::InvalidInput as i32,
        message: "Url need to specify https scheme".to_string(),
    });
}
```

The `#[cfg(not(feature = "http"))]` annotation means this entire check is **compiled out** when the `http` feature is enabled. The same pattern applies to `generate_from_flexible_args`: [4](#0-3) 

The execution environment entry point that calls both functions: [5](#0-4) 

### Impact Explanation

When the `http` feature is compiled into the adapter binary, the HTTPS-only enforcement is entirely absent. A canister can submit `http://` URLs to `ic00::HttpRequest` or `ic00::FlexibleHttpRequest`, and the adapter will execute them as plaintext HTTP requests. This exposes the canister HTTP outcall pipeline to MITM attacks on the plaintext channel — exactly the class of attack described in the external report. Even in production builds (without the `http` feature), the execution environment accepts and stores `http://` URLs in replicated state, consuming cycles before the adapter rejects them, creating a cycles-drain vector.

### Likelihood Explanation

Any canister developer can call the management canister's `http_request` method with an `http://` URL. The entry path requires no privileged access. In environments where the adapter is compiled with the `http` feature (e.g., local development, testnets, or any deployment that enables the feature), the plaintext HTTP request is executed without any scheme check. The feature flag exists in the production codebase and is tested: [6](#0-5) 

### Recommendation

Add URL scheme validation inside `generate_from_args` and `generate_from_flexible_args` in `rs/types/types/src/canister_http.rs`, rejecting any URL whose scheme is not `https`. This enforces the HTTPS-only policy at the consensus/execution layer rather than relying solely on the adapter. The adapter-level check should remain as defense-in-depth but must not be the sole enforcement point. The `#[cfg(not(feature = "http"))]` gate on the adapter check should be reviewed; if plaintext HTTP is intentionally allowed in some deployments, the execution environment must also be aware of this policy rather than silently accepting all schemes.

### Proof of Concept

1. Deploy a canister on a subnet with `http_requests` enabled.
2. Call the management canister's `http_request` method with `url: "http://attacker.example.com/steal"`.
3. Observe that `generate_from_args` accepts the request (only `validate_url_length` is called — no scheme check).
4. In a build compiled with `--features http`, the adapter's scheme check is compiled out and the plaintext HTTP request is executed.
5. In a production build, the adapter rejects it, but cycles are already consumed from the calling canister. [7](#0-6) [3](#0-2)

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

**File:** rs/types/types/src/canister_http.rs (L602-605)
```rust
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

**File:** rs/execution_environment/src/execution_environment.rs (L1270-1305)
```rust
            Ok(Ic00Method::HttpRequest) => match state.metadata.own_subnet_features.http_requests {
                true => match &msg {
                    CanisterCall::Request(request) => {
                        match CanisterHttpRequestArgs::decode(payload) {
                            Err(err) => ExecuteSubnetMessageResult::Finished {
                                response: Err(err),
                                refund: msg.take_cycles(),
                            },
                            Ok(args) => {
                                match CanisterHttpRequestContext::generate_from_args(
                                    state.time(),
                                    request.as_ref(),
                                    args,
                                    &registry_settings.node_ids,
                                    rng,
                                ) {
                                    Err(err) => ExecuteSubnetMessageResult::Finished {
                                        response: Err(err.into()),
                                        refund: msg.take_cycles(),
                                    },
                                    Ok(canister_http_request_context) => match self
                                        .try_add_http_context_to_replicated_state(
                                            canister_http_request_context,
                                            &mut state,
                                            request.as_ref(),
                                            since,
                                        ) {
                                        Err(err) => ExecuteSubnetMessageResult::Finished {
                                            response: Err(err),
                                            refund: msg.take_cycles(),
                                        },
                                        Ok(()) => ExecuteSubnetMessageResult::Processing,
                                    },
                                }
                            }
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
