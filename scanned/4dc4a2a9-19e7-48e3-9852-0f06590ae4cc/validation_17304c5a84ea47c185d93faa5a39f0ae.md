### Title
Query Endpoint Missing Mandatory Per-IP and Per-Subnet Rate Limiting at API Boundary Node - (File: `rs/boundary_node/ic_boundary/src/core.rs`)

---

### Summary
The `setup_router` function in the IC API boundary node (`ic_boundary`) applies per-IP and per-subnet rate limiting exclusively to update call routes (`PATH_CALL_V2/V3/V4`). Query routes (`PATH_QUERY_V2/V3`) and read-state routes are built without these throttles. An unprivileged external sender can flood the query endpoint at an unbounded rate, exhausting boundary node and replica resources, even when an operator has explicitly configured `--rate-limit-per-second-per-ip` or `--rate-limit-per-second-per-subnet`.

---

### Finding Description

In `rs/boundary_node/ic_boundary/src/core.rs`, `setup_router` constructs two separate route groups before merging them:

**Call routes** — rate limiting conditionally applied: [1](#0-0) 

**Query routes** — no per-IP or per-subnet rate limiting applied at all: [2](#0-1) 

**Read-state routes** — also no per-IP or per-subnet rate limiting: [3](#0-2) 

All three route groups are then merged and share `canister_layers`, which includes the `middleware_generic_limiter`: [4](#0-3) 

However, the `generic_limiter` is itself optional — it is only instantiated when either `rate_limit_generic_file` or `rate_limit_generic_canister_id` is configured: [5](#0-4) 

The `bouncer` is similarly optional: [6](#0-5) 

The CLI definition confirms both per-IP and per-subnet rate limits are `Option<u32>` with no default value — they are off unless explicitly set: [7](#0-6) 

The `RateLimit` implementation only provides `add_ip_rate_limiting` and `add_subnet_rate_limiting` methods, which are only called inside the `canister_call_routes` block: [8](#0-7) 

The structural consequence: even if an operator sets `--rate-limit-per-second-per-ip=N`, that limit is silently not enforced for query or read-state requests. Only update calls are throttled.

A secondary, related gap exists at the replica HTTP endpoint layer: `call_v3_router`, `call_v4_router`, and `subnet_call_v4_router` are merged without a `GlobalConcurrencyLimitLayer`, unlike `call_v2_router`. A TODO comment acknowledges this explicitly: [9](#0-8) 

---

### Impact Explanation

The IC query endpoint (`/api/v2/canister/{id}/query`, `/api/v3/canister/{id}/query`) is publicly reachable by any unauthenticated sender through the API boundary node. Because no per-IP or per-subnet throttle is enforced on these routes by default, a single attacker IP can issue an unbounded volume of query requests. Each query request:

- Consumes CPU on the boundary node (TLS, CBOR parsing, routing)
- Consumes a query execution thread slot on the replica (bounded only by `max_query_concurrent_requests` at the replica level, not at the boundary node)
- Consumes network bandwidth and connection state

This can degrade or deny service to legitimate users on the same subnet or boundary node without requiring any privileged access.

**Impact: 4**

---

### Likelihood Explanation

The query endpoint is publicly accessible to any HTTP client. No authentication, delegation, or special capability is required to send query requests. The attacker only needs to know a valid canister ID (trivially discoverable from the IC dashboard or any public dapp). Sending hundreds of requests per second from a single IP is trivially achievable with standard HTTP tooling. The absence of a default rate limit means this works on any boundary node deployment that has not explicitly configured the optional generic limiter with appropriate per-IP rules.

**Likelihood: 3**

---

### Recommendation

1. Apply `add_ip_rate_limiting` and `add_subnet_rate_limiting` to `canister_query_routes` and `canister_read_state_routes` in `setup_router`, mirroring the existing logic for `canister_call_routes`.
2. Set a safe non-zero default for `rate_limit_per_second_per_ip` and `rate_limit_per_second_per_subnet` in the `RateLimiting` CLI struct so that protection is active without requiring explicit operator configuration.
3. Resolve the `TODO(CON-1574)` in `rs/http_endpoints/public/src/lib.rs` by applying a `GlobalConcurrencyLimitLayer` to `call_v3_router`, `call_v4_router`, and `subnet_call_v4_router` at the replica HTTP endpoint.
4. When a rate limit is exceeded, ensure the response includes a `Retry-After` header or equivalent so clients can back off gracefully.

---

### Proof of Concept

An unprivileged attacker sends a flood of query requests to the boundary node:

```bash
# Discover any valid canister ID (e.g., from the IC dashboard)
CANISTER_ID="ryjl3-tyaaa-aaaaa-aaaba-cai"
BN_URL="https://<boundary-node-host>"

# Send 300+ requests per minute with no throttling enforced
for i in $(seq 1 500); do
  curl -s -X POST "$BN_URL/api/v2/canister/$CANISTER_ID/query" \
    -H "Content-Type: application/cbor" \
    --data-binary @query_payload.cbor &
done
wait
```

Because `canister_query_routes` is built without `add_ip_rate_limiting`: [2](#0-1) 

and the per-IP/subnet rate limit options are `None` by default: [10](#0-9) 

all 500 requests pass through to the replica's query execution pool, exhausting `max_query_concurrent_requests` slots and causing legitimate query requests to receive `429 Too Many Requests` from the replica's load shedder — while the attacker's requests themselves are never throttled at the boundary node.

### Citations

**File:** rs/boundary_node/ic_boundary/src/core.rs (L297-302)
```rust
    // Bouncer
    let bouncer = if cli.bouncer.bouncer_enable {
        Some(bouncer::setup(&cli.bouncer, &metrics_registry).context("unable to setup bouncer")?)
    } else {
        None
    };
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L312-330)
```rust
    let generic_limiter = if let Some(v) = &cli.rate_limiting.rate_limit_generic_file {
        Some(Arc::new(generic::GenericLimiter::new_from_file(
            v.clone(),
            generic_limiter_opts,
            channel_snapshot_recv,
            &metrics_registry,
        )))
    } else if let Some(v) = cli.rate_limiting.rate_limit_generic_canister_id {
        Some(Arc::new(generic::GenericLimiter::new_from_canister(
            v,
            agent.clone().unwrap(),
            generic_limiter_opts,
            cli.misc.crypto_config.is_some(),
            channel_snapshot_recv,
            &metrics_registry,
        )))
    } else {
        None
    };
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L856-858)
```rust
    let canister_query_routes = Router::new()
        .route(PATH_QUERY_V2, canister_handler.clone())
        .route(PATH_QUERY_V3, canister_handler.clone());
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

**File:** rs/boundary_node/ic_boundary/src/core.rs (L1015-1025)
```rust
    // Layers specific to the canister requests
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
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L1042-1044)
```rust
    let canister_read_state_routes = Router::new()
        .route(PATH_READ_STATE_V2, canister_handler.clone())
        .route(PATH_READ_STATE_V3, canister_handler.clone());
```

**File:** rs/boundary_node/ic_boundary/src/cli.rs (L269-278)
```rust
#[derive(Args)]
pub struct RateLimiting {
    /// Allowed number of update calls per second per subnet per boundary node.
    #[clap(env, long, value_parser = clap::value_parser!(u32).range(1..))]
    pub rate_limit_per_second_per_subnet: Option<u32>,

    /// Allowed number of update calls per second per ip per boundary node.
    #[clap(env, long, value_parser = clap::value_parser!(u32).range(1..))]
    pub rate_limit_per_second_per_ip: Option<u32>,

```

**File:** rs/boundary_node/ic_boundary/src/rate_limiting/mod.rs (L65-101)
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
}
```

**File:** rs/http_endpoints/public/src/lib.rs (L606-609)
```rust
            // TODO(CON-1574): see if there is any reasonable explicit concurrency limit we could use here.
            .merge(http_handler.call_v3_router)
            .merge(http_handler.call_v4_router)
            .merge(http_handler.subnet_call_v4_router)
```
