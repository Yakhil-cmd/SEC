### Title
Query Endpoint Signature Verification Not Rate Limited Per-IP at Boundary Node - (File: `rs/boundary_node/ic_boundary/src/core.rs`)

### Summary
The IC boundary node applies per-IP and per-subnet rate limiting exclusively to call (update) routes, not to query or read_state routes. An unprivileged attacker can flood the query endpoint with requests bearing invalid signatures, forcing expensive cryptographic signature verification at replica nodes without any per-IP throttle.

### Finding Description
In `setup_router` inside `rs/boundary_node/ic_boundary/src/core.rs`, `canister_call_routes` receives optional per-IP and per-subnet rate limiting via `add_ip_rate_limiting` / `add_subnet_rate_limiting`, while `canister_query_routes` is built with no such layer:

```rust
let canister_query_routes = Router::new()
    .route(PATH_QUERY_V2, canister_handler.clone())
    .route(PATH_QUERY_V3, canister_handler.clone());
// ← no rate limiting applied

let canister_call_routes = {
    let mut route = Router::new()...;
    if let Some(rl) = cli.rate_limiting.rate_limit_per_second_per_ip {
        route = RateLimit::try_from(rl).unwrap().add_ip_rate_limiting(route);
    }
    ...
};
``` [1](#0-0) 

The boundary node forwards every query request to a replica. At the replica, `query()` in `rs/http_endpoints/public/src/query.rs` calls `validator.validate_request()` inside a `spawn_blocking` task, which executes the full signature-verification pipeline: Ed25519/ECDSA/WebAuthn signature check, delegation-chain traversal (up to `MAXIMUM_NUMBER_OF_DELEGATIONS` links each verified cryptographically), and a registry root-of-trust lookup. [2](#0-1) [3](#0-2) [4](#0-3) 

All of this work happens before any rate-limiting decision is made for query traffic. The replica's only self-protection for query is a global concurrency cap (`max_query_concurrent_requests`), which limits total in-flight requests across all callers, not per-IP. [5](#0-4) 

The optional `Bouncer` middleware tracks aggregate request rate per IP and can eventually block an IP, but it is a coarse token-bucket applied globally across all routes and is not guaranteed to be enabled. [6](#0-5) 

### Impact Explanation
A single attacker IP can continuously submit query requests with syntactically valid CBOR bodies but invalid signatures. Each request consumes a `spawn_blocking` thread slot on the replica for cryptographic work. Because the global concurrency cap is shared, the attacker can saturate it, causing legitimate query requests to be shed with `429 Too Many Requests`. The attack is asymmetric: the attacker pays only network cost while the replica pays CPU cost for every verification attempt.

### Likelihood Explanation
The query endpoint (`/api/v2/canister/{id}/query`, `/api/v3/canister/{id}/query`) is publicly reachable through boundary nodes with no prior authentication. Crafting a CBOR-encoded query body with a plausible but invalid signature requires only knowledge of the public IC API format. The asymmetry between call routes (rate-limited) and query routes (not rate-limited) is a structural gap that a motivated attacker can exploit without any privileged access.

### Recommendation
Apply `add_ip_rate_limiting` (and optionally `add_subnet_rate_limiting`) to `canister_query_routes` and read_state routes in `setup_router`, mirroring the treatment of call routes. Additionally, consider incrementing per-IP counters even on signature-verification failures so that repeated bad-signature submissions accelerate throttling rather than being invisible to the rate limiter.

### Proof of Concept
1. Construct a valid CBOR-encoded `HttpRequestEnvelope<HttpQueryContent>` targeting any canister, with a non-anonymous sender, a valid-looking `sender_pubkey`, and a random (invalid) `sender_sig`.
2. Send this request in a tight loop to `POST /api/v2/canister/<canister_id>/query` on a boundary node.
3. The boundary node forwards each request to a replica with no per-IP check.
4. The replica spawns a blocking task for each, running `validate_request` → `validate_user_id_and_signature` → `validate_signature` → `verify_basic_sig_by_public_key`, consuming a thread and CPU time before returning `400 Bad Request`.
5. Once `max_query_concurrent_requests` slots are occupied by attacker requests, legitimate users receive `429 Too Many Requests`. [1](#0-0) [7](#0-6) [8](#0-7) [5](#0-4)

### Citations

**File:** rs/boundary_node/ic_boundary/src/core.rs (L856-879)
```rust
    let canister_query_routes = Router::new()
        .route(PATH_QUERY_V2, canister_handler.clone())
        .route(PATH_QUERY_V3, canister_handler.clone());

    let canister_call_routes = {
        let mut route = Router::new()
            .route(PATH_CALL_V2, canister_handler.clone())
            .route(PATH_CALL_V3, canister_handler.clone())
            .route(PATH_CALL_V4, canister_handler.clone());

        // will panic if ip_rate_limit is Some(0)
        if let Some(rl) = cli.rate_limiting.rate_limit_per_second_per_ip {
            route = RateLimit::try_from(rl).unwrap().add_ip_rate_limiting(route);
        }

        // will panic if subnet_rate_limit is Some(0)
        if let Some(rl) = cli.rate_limiting.rate_limit_per_second_per_subnet {
            route = RateLimit::try_from(rl)
                .unwrap()
                .add_subnet_rate_limiting(route)
        }

        route
    };
```

**File:** rs/http_endpoints/public/src/query.rs (L189-265)
```rust
pub(crate) async fn query(
    axum::extract::Path(id): axum::extract::Path<PrincipalId>,
    State(QueryService {
        log,
        node_id,
        registry_client,
        time_source,
        validator,
        health_status,
        signer,
        nns_delegation_reader,
        additional_root_of_trust,
        query_execution_service,
        subnet_id,
        version,
    }): State<QueryService>,
    WithTimeout(Cbor(request)): WithTimeout<Cbor<HttpRequestEnvelope<HttpQueryContent>>>,
) -> impl IntoResponse {
    if health_status.load() != ReplicaHealthStatus::Healthy {
        let status = StatusCode::SERVICE_UNAVAILABLE;
        let text = format!(
            "Replica is unhealthy: {:?}. Check the /api/v2/status for more information.",
            health_status.load(),
        );
        return (status, text).into_response();
    }

    let registry_version = registry_client.get_latest_version();

    // Convert the message to a strongly-typed struct, making structural validations
    // on the way.
    let request = match HttpRequest::<Query>::try_from(request) {
        Ok(request) => request,
        Err(e) => {
            let status = StatusCode::BAD_REQUEST;
            let text = format!("Malformed request: {e:?}");
            return (status, text).into_response();
        }
    };
    let canister_id = request.content().canister_id();

    // Validate effective destination.
    match version {
        Version::V2 | Version::V3 => {
            let effective_canister_id = CanisterId::unchecked_from_principal(id);
            if canister_id != CanisterId::ic_00() && canister_id != effective_canister_id {
                let status = StatusCode::BAD_REQUEST;
                let text = format!(
                    "Specified canister ID {canister_id} does not match effective canister ID in URL {effective_canister_id}"
                );
                return (status, text).into_response();
            }
        }
        Version::SubnetV3 => {
            let effective_subnet_id = SubnetId::from(id);
            if effective_subnet_id != subnet_id {
                let status = StatusCode::BAD_REQUEST;
                let text = format!(
                    "Specified subnet ID {effective_subnet_id} does not match the subnet ID of this node {subnet_id}"
                );
                return (status, text).into_response();
            }
            if canister_id != CanisterId::ic_00()
                || request.content().method_name != "list_canisters"
            {
                let status = StatusCode::BAD_REQUEST;
                let text = format!(
                    "Subnet query endpoint only accepts queries to the management canister ({}) 'list_canisters' method, got canister_id={} method_name='{}'",
                    CanisterId::ic_00(),
                    canister_id,
                    request.content().method_name
                );
                return (status, text).into_response();
            }
        }
    }

```

**File:** rs/validator/src/ingress_validation.rs (L635-703)
```rust
fn validate_signature<R: RootOfTrustProvider>(
    validator: &dyn IngressSigVerifier,
    message_id: &MessageId,
    signature: &UserSignature,
    current_time: Time,
    root_of_trust_provider: &R,
) -> Result<CanisterIdSet, RequestValidationError>
where
    R::Error: std::error::Error,
{
    validate_sender_delegation_length(&signature.sender_delegation)?;
    validate_sender_delegation_expiry(&signature.sender_delegation, current_time)?;
    let empty_vec = Vec::new();
    let signed_delegations = signature.sender_delegation.as_ref().unwrap_or(&empty_vec);

    let (pubkey, targets) = validate_delegations(
        validator,
        signed_delegations.as_slice(),
        signature.signer_pubkey.clone(),
        root_of_trust_provider,
    )?;

    let (pk, pk_type) = public_key_from_bytes(&pubkey).map_err(InvalidSignature)?;

    match pk_type {
        KeyBytesContentType::EcdsaP256PublicKeyDerWrappedCose
        | KeyBytesContentType::Ed25519PublicKeyDerWrappedCose
        | KeyBytesContentType::RsaSha256PublicKeyDerWrappedCose => {
            let webauthn_sig = WebAuthnSignature::try_from(signature.signature.as_slice())
                .map_err(WebAuthnError)
                .map_err(InvalidSignature)?;
            validate_webauthn_sig(validator, &webauthn_sig, message_id, &pk)
                .map_err(WebAuthnError)
                .map_err(InvalidSignature)?;
            Ok(targets)
        }
        KeyBytesContentType::Ed25519PublicKeyDer
        | KeyBytesContentType::EcdsaP256PublicKeyDer
        | KeyBytesContentType::EcdsaSecp256k1PublicKeyDer => {
            let basic_sig = BasicSigOf::from(BasicSig(signature.signature.clone()));
            validate_signature_plain(validator, message_id, &basic_sig, &pk)
                .map_err(InvalidSignature)?;
            Ok(targets)
        }
        KeyBytesContentType::IcCanisterSignatureAlgPublicKeyDer => {
            let canister_sig = CanisterSigOf::from(CanisterSig(signature.signature.clone()));
            verify_canister_sig_with_fallback!(
                validator,
                &canister_sig,
                message_id,
                &pk,
                root_of_trust_provider,
                |e| InvalidSignature(InvalidCanisterSignature(e.to_string())),
                |e: <R as RootOfTrustProvider>::Error| InvalidSignature(InvalidCanisterSignature(
                    e.to_string()
                ))
            );
            Ok(targets)
        }
        KeyBytesContentType::RsaSha256PublicKeyDer => {
            Err(RequestValidationError::InvalidSignature(
                AuthenticationError::InvalidBasicSignature(CryptoError::AlgorithmNotSupported {
                    algorithm: AlgorithmId::RsaSha256,
                    reason: "RSA signatures are not allowed except in webauthn context".to_owned(),
                }),
            ))
        }
    }
}
```

**File:** rs/validator/src/ingress_validation.rs (L721-753)
```rust
fn validate_delegations<R: RootOfTrustProvider>(
    validator: &dyn IngressSigVerifier,
    signed_delegations: &[SignedDelegation],
    mut pubkey: Vec<u8>,
    root_of_trust_provider: &R,
) -> Result<(Vec<u8>, CanisterIdSet), RequestValidationError>
where
    R::Error: std::error::Error,
{
    ensure_delegations_does_not_contain_cycles(&pubkey, signed_delegations)?;
    ensure_delegations_does_not_contain_too_many_targets(signed_delegations)?;
    // Initially, assume that the delegations target all possible canister IDs.
    let mut targets = CanisterIdSet::all();

    for sd in signed_delegations {
        let delegation = sd.delegation();
        let signature = sd.signature();

        let new_targets = validate_delegation(
            validator,
            signature,
            delegation,
            &pubkey,
            root_of_trust_provider,
        )
        .map_err(InvalidDelegation)?;
        // Restrict the canister targets to the ones specified in the delegation.
        targets = targets.intersect(new_targets);
        pubkey = delegation.pubkey().to_vec();
    }

    Ok((pubkey, targets))
}
```

**File:** rs/config/src/http_handler.rs (L57-60)
```rust
    /// Serving at most `max_call_concurrent_requests` requests concurrently for each endpoint:
    /// `/api/v2/query`, `/api/v3/query`
    pub max_query_concurrent_requests: usize,

```

**File:** rs/boundary_node/ic_boundary/src/bouncer/mod.rs (L278-300)
```rust
pub async fn middleware(
    State(bouncer): State<Arc<Bouncer>>,
    request: Request<Body>,
    next: Next,
) -> Result<impl IntoResponse, ErrorCause> {
    // Attempt to extract client's IP from the request
    let ip = request
        .extensions()
        .get::<Arc<ConnInfo>>()
        .map(|x| x.remote_addr.ip());

    if let Some(v) = ip {
        if !bouncer.acquire_token(v) {
            return Err(ErrorCause::RateLimited(RateLimitCause::Bouncer));
        }
    } else {
        // This should not really happen ever, unless somebody enables bouncer when running with Unix socket.
        // Maybe we should check that and forbid or add IP extraction using X-Real-IP & friends headers.
        return Err(ErrorCause::Other("Unable to extract client's IP".into()));
    }

    Ok(next.run(request).await)
}
```

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/mod.rs (L65-100)
```rust
impl RateLimit {
    /// Allow requests_per_second requests per IP
    pub fn add_ip_rate_limiting(&self, router: Router) -> Router {
        let interval = Duration::from_secs(1)
            .checked_div(self.requests_per_second)
            .unwrap();

        let governor_conf = GovernorConfigBuilder::default()
            .per_nanosecond(interval.as_nanos().try_into().unwrap())
            .burst_size(self.requests_per_second)
            .key_extractor(IpKeyExtractor)
            .finish()
            .unwrap();

        router.layer(ServiceBuilder::new().layer(GovernorLayer {
            config: Arc::new(governor_conf),
        }))
    }

    /// Allow requests_per_second requests per subnet
    pub fn add_subnet_rate_limiting(&self, router: Router) -> Router {
        let interval = Duration::from_secs(1)
            .checked_div(self.requests_per_second)
            .unwrap();

        let governor_conf = GovernorConfigBuilder::default()
            .per_nanosecond(interval.as_nanos().try_into().unwrap())
            .burst_size(self.requests_per_second)
            .key_extractor(SubnetKeyExtractor)
            .finish()
            .unwrap();

        router.layer(ServiceBuilder::new().layer(GovernorLayer {
            config: Arc::new(governor_conf),
        }))
    }
```
