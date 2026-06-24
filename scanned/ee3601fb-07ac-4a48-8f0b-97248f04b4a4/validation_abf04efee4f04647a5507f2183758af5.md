### Title
Cache Key Collision Between QueryV2 and QueryV3 Allows Cross-Version Response Poisoning — (`rs/boundary_node/ic_boundary/src/routes.rs`, `rs/boundary_node/ic_boundary/src/http/middleware/cache.rs`)

---

### Summary

`RequestContext::Hash` and `RequestContext::PartialEq` deliberately omit `request_type` from their implementations. Both `QueryV2` and `QueryV3` pass the `BypasserIC` check (both satisfy `is_query()`), and both routes share the same `CacheState` instance. This means two requests that differ only in `request_type` (e.g., one hitting `/api/v2/canister/{id}/query` and one hitting `/api/v3/canister/{id}/query`) will collide on the same cache key, causing the boundary node to serve a response formatted for one API version to a client of the other.

---

### Finding Description

**Root cause — missing `request_type` in cache key:** [1](#0-0) 

`Hash` feeds only `canister_id`, `sender`, `method_name`, `ingress_expiry`, and `arg` into the hasher. `PartialEq` compares the same five fields. `request_type` is absent from both.

**Both QueryV2 and QueryV3 are admitted by the bypasser:** [2](#0-1) 

`is_query()` returns `true` for `QueryV2`, `QueryV3`, and `QuerySubnetV3`: [3](#0-2) 

**Both routes share the same `CacheState` instance:** [4](#0-3) 

Both `PATH_QUERY_V2` and `PATH_QUERY_V3` are merged into `canister_query_routes`, and the same `canister_layers` (containing `cache_middleware`) is applied to the merged set: [5](#0-4) 

**The replica produces structurally different responses for V2 vs V3:**

QueryV2 embeds an NNS delegation with canister ranges in **flat** format; QueryV3 embeds them in **tree** format. This is a documented, tested protocol invariant: [6](#0-5) 

The test `interlaced_v2_and_v3_query_requests` (lines 527–532) exists precisely to guard against cross-version cache contamination — but only at the replica level, not at the boundary node level: [7](#0-6) 

---

### Impact Explanation

The boundary node caches the raw CBOR-encoded HTTP response body returned by the replica. When a QueryV2 response is cached and a QueryV3 request with identical `(canister_id, sender, method_name, ingress_expiry, arg)` arrives, the boundary node serves the QueryV2 body. The QueryV3 client receives a certificate whose NNS delegation uses flat-format canister ranges instead of the expected tree-format. A conformant QueryV3 client (or SDK) that strictly validates the delegation format will reject the response, causing a client-visible failure. The reverse (QueryV3 cached, QueryV2 client served) similarly breaks flat-format delegation validation.

The impact is **availability / correctness**: clients receive cryptographically valid but protocol-version-mismatched responses. There is no confidentiality or integrity violation — the node signature over the response content remains valid.

---

### Likelihood Explanation

Any unprivileged client can trigger this by:
1. Sending a QueryV2 POST to `/api/v2/canister/{id}/query` with a chosen `(canister_id, sender, method_name, ingress_expiry, arg)` tuple to populate the cache.
2. Sending a QueryV3 POST to `/api/v3/canister/{id}/query` with the identical tuple within the TTL window (default 1 s).

No special privileges, keys, or network position are required. The collision is deterministic and locally testable.

---

### Recommendation

Include `request_type` in both `Hash` and `PartialEq` for `RequestContext`:

```rust
// In Hash::hash()
self.request_type.hash(state);

// In PartialEq::eq()
&& self.request_type == other.request_type
```

The comment at line 91–93 of `routes.rs` already states the invariant that both impls must operate on the same fields — `request_type` simply needs to be added to both.

---

### Proof of Concept

```rust
let ctx_v2 = RequestContext {
    request_type: RequestType::QueryV2,
    canister_id: Some(principal),
    sender: Some(ANONYMOUS_PRINCIPAL),
    method_name: Some("foo".into()),
    ingress_expiry: Some(0),
    arg: Some(vec![1, 2, 3]),
    ..Default::default()
};
let ctx_v3 = RequestContext { request_type: RequestType::QueryV3, ..ctx_v2.clone() };

// These assert today:
assert_eq!(ctx_v2, ctx_v3);
assert_eq!(hash_of(&ctx_v2), hash_of(&ctx_v3));

// Drive ctx_v2 through cache_middleware → CacheStatus::Miss, response body = "V2_BODY"
// Drive ctx_v3 through cache_middleware → CacheStatus::Hit, response body = "V2_BODY"  ← wrong version
```

### Citations

**File:** rs/boundary_node/ic_boundary/src/routes.rs (L91-126)
```rust
/// Hash and Eq are implemented for request caching
/// They should both work on the same fields so that
/// k1 == k2 && hash(k1) == hash(k2)
impl Hash for RequestContext {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.canister_id.hash(state);
        self.sender.hash(state);
        self.method_name.hash(state);
        self.ingress_expiry.hash(state);

        // Hash http_request if it's present, arg otherwise
        // They're mutually exclusive
        if self.http_request.is_some() {
            self.http_request.hash(state);
        } else {
            self.arg.hash(state);
        }
    }
}

impl PartialEq for RequestContext {
    fn eq(&self, other: &Self) -> bool {
        let r = self.canister_id == other.canister_id
            && self.sender == other.sender
            && self.method_name == other.method_name
            && self.ingress_expiry == other.ingress_expiry;

        // Same as in hash()
        if self.http_request.is_some() {
            r && self.http_request == other.http_request
        } else {
            r && self.arg == other.arg
        }
    }
}
impl Eq for RequestContext {}
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/cache.rs (L74-76)
```rust
        Ok(if !ctx.request_type.is_query() {
            // We cache only Query
            Some(BypassReasonIC::IncorrectRequestType)
```

**File:** rs/boundary_node/ic_boundary/src/http/mod.rs (L62-64)
```rust
    pub const fn is_query(&self) -> bool {
        matches!(self, Self::QueryV2 | Self::QueryV3 | Self::QuerySubnetV3)
    }
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L856-858)
```rust
    let canister_query_routes = Router::new()
        .route(PATH_QUERY_V2, canister_handler.clone())
        .route(PATH_QUERY_V3, canister_handler.clone());
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L1016-1049)
```rust
    let canister_layers = ServiceBuilder::new()
        .layer(middleware::from_fn(validate::validate_request))
        .layer(middleware::from_fn(validate::validate_canister_request))
        .layer(common_layers.clone())
        .layer(middleware_subnet_lookup.clone())
        .layer(middleware_generic_limiter.clone())
        .layer(option_layer(cache_state.map(|x| {
            middleware::from_fn_with_state(x.clone(), cache_middleware)
        })))
        .layer(middleware_retry.clone());

    // Layers specific to the subnet requests
    let middleware_subnet_read_state_cache = option_layer(
        subnet_read_state_cache_state
            .map(|x| middleware::from_fn_with_state(x, subnet_read_state_cache_middleware)),
    );

    let subnet_layers = ServiceBuilder::new()
        .layer(middleware::from_fn(validate::validate_request))
        .layer(middleware::from_fn(validate::validate_subnet_request))
        .layer(common_layers)
        .layer(middleware_subnet_read_state_cache)
        .layer(middleware_subnet_lookup)
        .layer(middleware_generic_limiter)
        .layer(middleware_retry);

    let canister_read_state_routes = Router::new()
        .route(PATH_READ_STATE_V2, canister_handler.clone())
        .route(PATH_READ_STATE_V3, canister_handler.clone());

    let canister_routes = canister_query_routes
        .merge(canister_call_routes)
        .merge(canister_read_state_routes)
        .layer(canister_layers);
```

**File:** rs/tests/networking/nns_delegation_test.rs (L479-524)
```rust
/// For `api/v2/canister/{canister_id}/query` we pass valid delegations with
/// canister ranges in the flat format to the canister.
fn query_v2_passes_correct_delegation_to_canister(env: TestEnv, subnet_type: SubnetType) {
    let canister_id = get_installed_canister_id(&env, subnet_type);
    let (subnet, node) = get_subnet_and_node(&env, subnet_type);
    let arg = vec![];

    let response: QueryResponse = block_on(send(
        &node,
        format!("api/v2/canister/{canister_id}/query"),
        sign_envelope(&query_content(canister_id, arg)),
    ));
    let certificate: Certificate = serde_cbor::from_slice(&response.reply.arg).unwrap();

    validate_delegation(
        &env,
        certificate.delegation.as_ref(),
        subnet.subnet_id,
        subnet_type,
        Some(canister_id),
        CertificateDelegationFormat::Flat,
    );
}

/// For `api/v3/canister/{canister_id}/query` we pass valid delegations with
/// canister ranges in the tree format to the canister.
fn query_v3_passes_correct_delegation_to_canister(env: TestEnv, subnet_type: SubnetType) {
    let canister_id = get_installed_canister_id(&env, subnet_type);
    let (subnet, node) = get_subnet_and_node(&env, subnet_type);
    let arg = vec![];

    let response: QueryResponse = block_on(send(
        &node,
        format!("api/v3/canister/{canister_id}/query"),
        sign_envelope(&query_content(canister_id, arg)),
    ));
    let certificate: Certificate = serde_cbor::from_slice(&response.reply.arg).unwrap();

    validate_delegation(
        &env,
        certificate.delegation.as_ref(),
        subnet.subnet_id,
        subnet_type,
        Some(canister_id),
        CertificateDelegationFormat::Tree,
    );
```

**File:** rs/tests/networking/nns_delegation_test.rs (L527-533)
```rust
/// Run query tests several times sequentially to check that we don't return incorrect cached response.
fn interlaced_v2_and_v3_query_requests(env: TestEnv, subnet_type: SubnetType) {
    for _ in 0..10 {
        query_v2_passes_correct_delegation_to_canister(env.clone(), subnet_type);
        query_v3_passes_correct_delegation_to_canister(env.clone(), subnet_type);
    }
}
```
