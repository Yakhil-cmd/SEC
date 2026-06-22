### Title
IP/Subnet Rate Limiting Middleware Inconsistently Applied — `PATH_SUBNET_CALL_V4` Unprotected - (File: `rs/boundary_node/ic_boundary/src/core.rs`)

### Summary
The `ic-boundary` boundary node applies per-IP and per-subnet rate limiting only to canister call routes (`PATH_CALL_V2`, `PATH_CALL_V3`, `PATH_CALL_V4`). The subnet call route `PATH_SUBNET_CALL_V4` (`/api/v4/subnet/{subnet_id}/call`) is assembled into `subnet_routes` without any of these rate-limiting layers, allowing an unprivileged external sender to bypass the configured call rate limits entirely by using the subnet-addressed call endpoint.

### Finding Description

In `setup_router()`, the per-IP and per-subnet rate-limiting middleware is applied exclusively to `canister_call_routes`:

```rust
// rs/boundary_node/ic_boundary/src/core.rs  lines 860-879
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

The subnet routes, including `PATH_SUBNET_CALL_V4`, are assembled separately with no equivalent rate-limiting applied:

```rust
// rs/boundary_node/ic_boundary/src/core.rs  lines 1051-1056
let subnet_routes = Router::new()
    .route(PATH_SUBNET_READ_STATE_V2, subnet_handler.clone())
    .route(PATH_SUBNET_READ_STATE_V3, subnet_handler.clone())
    .route(PATH_SUBNET_QUERY_V3, subnet_handler.clone())
    .route(PATH_SUBNET_CALL_V4, subnet_handler.clone())  // ❌ No rate limiting
    .layer(subnet_layers);
```

The CLI documentation for these rate-limiting flags explicitly states they apply to "update calls":

```
/// Allowed number of update calls per second per subnet per boundary node.
rate_limit_per_second_per_subnet: Option<u32>,

/// Allowed number of update calls per second per ip per boundary node.
rate_limit_per_second_per_ip: Option<u32>,
```

`PATH_SUBNET_CALL_V4` (`/api/v4/subnet/{subnet_id}/call`) is a valid update call endpoint that submits ingress messages to the IC, just like the canister-addressed call endpoints. It is reachable by any unprivileged external sender.

### Impact Explanation

When an operator configures `rate_limit_per_second_per_ip` or `rate_limit_per_second_per_subnet`, the intent is to throttle update call traffic at the boundary node. An attacker who knows about the subnet-addressed call endpoint can bypass both rate limits entirely by sending all traffic to `/api/v4/subnet/{subnet_id}/call` instead of `/api/v{2,3,4}/canister/{canister_id}/call`. This allows:

- **Ingress pool flooding**: Unlimited update calls can be submitted per IP or per subnet, exhausting the ingress pool and causing `SERVICE_UNAVAILABLE` for legitimate users.
- **Targeted subnet DoS**: A single IP can flood a specific subnet's ingress pool without being throttled, since the subnet rate limiter is also absent from this path.
- **Rate-limit policy bypass**: Any operator-configured call-rate policy is silently ineffective for the subnet call path.

### Likelihood Explanation

The `PATH_SUBNET_CALL_V4` endpoint is publicly documented as part of the IC HTTP API spec and is reachable by any external sender without authentication. The bypass requires only knowledge of the alternative URL path. The generic rate limiter (`middleware_generic_limiter`) is present in `subnet_layers` and can be configured to cover this path via YAML rules, but the simpler, always-on per-IP/per-subnet rate limiters are structurally absent. An attacker performing a targeted DoS would naturally probe all available call endpoints.

### Recommendation

Apply the same per-IP and per-subnet rate-limiting layers to `PATH_SUBNET_CALL_V4` as are applied to the canister call routes. The simplest fix is to extract the rate-limiting logic into a shared helper and apply it to both `canister_call_routes` and the subnet call route before they are merged, or to build a unified `call_routes` group that includes all call endpoints.

### Proof of Concept

**Protected path** (`/api/v2/canister/{id}/call`):
```
POST /api/v2/canister/aaaaa-aa/call  → 429 Too Many Requests (after N requests)
```

**Unprotected path** (`/api/v4/subnet/{id}/call`):
```
POST /api/v4/subnet/<subnet_id>/call  → 202 Accepted (unlimited, rate limit never fires)
```

Root cause in `setup_router()`: [1](#0-0) 

Subnet routes assembled without rate limiting: [2](#0-1) 

Rate-limit CLI flags documented as applying to "update calls" (implying all call endpoints): [3](#0-2) 

`PATH_SUBNET_CALL_V4` path constant definition: [4](#0-3)

### Citations

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

**File:** rs/boundary_node/ic_boundary/src/core.rs (L1051-1056)
```rust
    let subnet_routes = Router::new()
        .route(PATH_SUBNET_READ_STATE_V2, subnet_handler.clone())
        .route(PATH_SUBNET_READ_STATE_V3, subnet_handler.clone())
        .route(PATH_SUBNET_QUERY_V3, subnet_handler.clone())
        .route(PATH_SUBNET_CALL_V4, subnet_handler.clone())
        .layer(subnet_layers);
```

**File:** rs/boundary_node/ic_boundary/src/cli.rs (L270-277)
```rust
pub struct RateLimiting {
    /// Allowed number of update calls per second per subnet per boundary node.
    #[clap(env, long, value_parser = clap::value_parser!(u32).range(1..))]
    pub rate_limit_per_second_per_subnet: Option<u32>,

    /// Allowed number of update calls per second per ip per boundary node.
    #[clap(env, long, value_parser = clap::value_parser!(u32).range(1..))]
    pub rate_limit_per_second_per_ip: Option<u32>,
```

**File:** rs/boundary_node/ic_boundary/src/http/mod.rs (L22-22)
```rust
pub const PATH_SUBNET_CALL_V4: &str = "/api/v4/subnet/{subnet_id}/call";
```
