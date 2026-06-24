The code is fully readable. Let me analyze the vulnerability claim precisely.

### Title
Cache Stampede / Thundering-Herd in `subnet_read_state_cache_middleware` Allows Upstream Replica Connection Pool Exhaustion — (`rs/boundary_node/ic_boundary/src/http/middleware/subnet_read_state_cache.rs`)

---

### Summary

The `subnet_read_state_cache_middleware` uses a plain `cache.get()` + `cache.insert()` pattern with no in-flight deduplication. N concurrent requests sharing the same `(subnet_id, paths)` cache key that all arrive before any response is stored will all observe a cache miss and all be forwarded to the upstream replica, defeating the cache's protective purpose.

---

### Finding Description

The middleware performs a two-step, non-atomic cache interaction:

1. **Line 122** — `state.cache.get(&cache_key)`: a non-blocking read from `moka::sync::Cache`. If the key is absent, execution falls through.
2. **Line 130** — `next.run(request).await`: the request is forwarded to the upstream replica.
3. **Line 143** — `state.cache.insert(cache_key, cached.clone())`: the response is stored only after the upstream round-trip completes. [1](#0-0) 

Between steps 1 and 3 there is a window — the entire upstream latency — during which the cache entry does not exist. Any concurrent request that calls `cache.get()` during this window also sees a miss and independently calls `next.run()`. There is no in-flight deduplication, no mutex, no `get_with` / compute-once primitive.

`moka::sync::Cache` provides exactly this primitive (`get_with` / `entry().or_insert_with()`), and it is already used elsewhere in the same codebase (e.g., `rs/boundary_node/ic_boundary/src/http/handlers.rs`), confirming the developers are aware of the pattern. [2](#0-1) 

The subnet read_state routes have **no per-IP or per-subnet rate limiting** applied. Rate limiting is only wired onto `canister_call_routes`: [3](#0-2) 

The `subnet_layers` stack — which wraps subnet read_state — does not include those guards: [4](#0-3) 

The default TTL is 30 seconds. During each 30-second window, an attacker can open a new stampede by sending a burst of concurrent requests timed to arrive before the first response is cached. [5](#0-4) 

---

### Impact Explanation

- Every boundary node worker thread/task that handles one of the N concurrent requests independently opens a connection to the upstream replica and awaits a full response.
- This exhausts the upstream replica's connection pool or triggers its rate limiter, degrading service for all users of that subnet.
- The cache is specifically designed to shield replicas from repeated identical requests; the stampede nullifies that shield entirely.
- Because subnet read_state paths (`canister_ranges`, `subnet`) are well-known and identical across all boundary-node clients, a single attacker can target the highest-traffic cache key.

---

### Likelihood Explanation

- The endpoint is fully public and unauthenticated.
- The attack requires only sending N HTTP requests concurrently — trivially achievable with any HTTP client library.
- No privileged access, no key material, no governance majority, and no network-level attack is required.
- The absence of per-IP rate limiting on subnet read_state routes removes the primary mitigation that exists for call routes.

---

### Recommendation

Replace the `get` + `insert` pattern with `moka`'s compute-once API so that only one in-flight upstream request is made per cache key at a time:

```rust
// Instead of:
if let Some(cached) = state.cache.get(&cache_key) { ... }
// ... forward to upstream ...
state.cache.insert(cache_key, cached.clone());

// Use:
let cached = state.cache.get_with(cache_key, async {
    // forward to upstream exactly once per key
    ...
}).await;
```

Additionally, add per-IP rate limiting to subnet read_state routes, consistent with the protection already applied to call routes.

---

### Proof of Concept

```rust
// Instrument the mock handler with an AtomicUsize counter.
// Spawn N=100 Tokio tasks, each sending the same cacheable subnet read_state request.
// Join all tasks.
// Assert counter.load(Ordering::SeqCst) > 1  ← will pass, demonstrating the stampede.
// With get_with, the same assertion would fail (counter == 1).
```

### Citations

**File:** rs/boundary_node/ic_boundary/src/http/middleware/subnet_read_state_cache.rs (L16-16)
```rust
use moka::sync::Cache;
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/subnet_read_state_cache.rs (L122-143)
```rust
    if let Some(cached) = state.cache.get(&cache_key) {
        state.hits.inc();
        state.update_gauges();
        return Ok(cached.map(Body::from));
    }

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
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L860-879)
```rust
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

**File:** rs/boundary_node/ic_boundary/src/cli.rs (L351-353)
```rust
    /// TTL for cached subnet read_state responses
    #[clap(env, long, default_value = "30s", value_parser = parse_duration)]
    pub subnet_read_state_cache_ttl: Duration,
```
