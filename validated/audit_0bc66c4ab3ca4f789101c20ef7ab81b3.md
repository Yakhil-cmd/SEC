### Title
Query Endpoint Lacks Per-IP Rate Limiting While Call Endpoint Rate Limiting Is Opt-In and Disabled by Default - (File: rs/boundary_node/ic_boundary/src/core.rs)

---

### Summary

The boundary node router in `rs/boundary_node/ic_boundary/src/core.rs` applies **no per-IP or per-subnet rate limiting to query routes** (`/api/v2/canister/{id}/query`, `/api/v3/canister/{id}/query`). For call routes, rate limiting exists in code but is **opt-in and disabled by default** — both `rate_limit_per_second_per_ip` and `rate_limit_per_second_per_subnet` are `Option<u32>` with no default value, meaning they are `None` unless explicitly configured. An unprivileged user can flood query requests from a single IP, forcing the replica to execute up to 5 billion Wasm instructions per query with no per-sender throttle.

---

### Finding Description

In `setup_router`, the query routes are constructed with no rate-limiting layer at all:

```rust
let canister_query_routes = Router::new()
    .route(PATH_QUERY_V2, canister_handler.clone())
    .route(PATH_QUERY_V3, canister_handler.clone());
``` [1](#0-0) 

For call routes, rate limiting is conditionally applied only when the CLI flags are `Some(...)`:

```rust
if let Some(rl) = cli.rate_limiting.rate_limit_per_second_per_ip {
    route = RateLimit::try_from(rl).unwrap().add_ip_rate_limiting(route);
}
if let Some(rl) = cli.rate_limiting.rate_limit_per_second_per_subnet {
    route = RateLimit::try_from(rl).unwrap().add_subnet_rate_limiting(route)
}
``` [2](#0-1) 

Both CLI fields are declared as `Option<u32>` with no `default_value`, meaning they default to `None` and rate limiting is **off by default**:

```rust
pub rate_limit_per_second_per_subnet: Option<u32>,
pub rate_limit_per_second_per_ip: Option<u32>,
``` [3](#0-2) 

The bouncer (IP-level firewall ban) is also opt-in via `--bouncer-enable`: [4](#0-3) 

The generic limiter is similarly optional: [5](#0-4) 

All three query routes, call routes, and read-state routes are merged and share only the optional common layers (bouncer, generic limiter, load shedders): [6](#0-5) 

On the replica side, each query call executes Wasm up to `MAX_INSTRUCTIONS_PER_QUERY_MESSAGE = 5 * B` (5 billion) instructions: [7](#0-6) 

The replica's public HTTP endpoint (`rs/http_endpoints/public/src/query.rs`) has no per-IP rate limiting either — only a health check and load shedding via the ingress pool throttler, which does not apply to query calls since queries bypass the ingress pool entirely: [8](#0-7) 

---

### Impact Explanation

A single unprivileged user can send an unbounded stream of query requests to any canister through the boundary node. Each query triggers full Wasm execution on the replica (up to 5B instructions, roughly 2.5 seconds of CPU on a 2 GHz core). Because query calls bypass the ingress pool and its throttler, the only backstop is the optional load shedder — which is also disabled by default. A sustained flood from one IP saturates the replica's query thread pool, causing legitimate queries to time out or be shed, effectively denying service to all users of that subnet without requiring any privileged access or coordination.

---

### Likelihood Explanation

The attack requires only an HTTP client and knowledge of any deployed canister ID — both trivially available. The boundary node is publicly reachable on port 443. No authentication, cycles, or special role is needed to send query calls. The default deployment configuration has all rate-limiting flags unset (`None`), so the vulnerable state is the out-of-the-box configuration. A developer with a buggy application (e.g., an infinite retry loop on query failures) can trigger this accidentally, matching the NuCypher exploit scenario exactly.

---

### Recommendation

1. **Apply per-IP rate limiting to query routes unconditionally**, mirroring the existing `add_ip_rate_limiting` mechanism already implemented for call routes in `rs/boundary_node/ic_boundary/src/rate_limiting/mod.rs`.
2. **Set safe non-`None` defaults** for `rate_limit_per_second_per_ip` and `rate_limit_per_second_per_subnet` in `rs/boundary_node/ic_boundary/src/cli.rs` so that rate limiting is active in the default deployment.
3. **Enable the bouncer by default** or document a mandatory minimum configuration for production deployments.

---

### Proof of Concept

```
# Flood query calls to any canister from a single IP with no rate limit applied
# (boundary node deployed with default flags — no --rate-limit-per-second-per-ip,
#  no --bouncer-enable, no --rate-limit-generic-canister-id)

CANISTER_ID="<any valid canister id>"
BN_HOST="https://<boundary-node-host>"

while true; do
  curl -s -X POST \
    -H "Content-Type: application/cbor" \
    --data-binary @query_payload.cbor \
    "$BN_HOST/api/v2/canister/$CANISTER_ID/query" &
done
```

Each concurrent request causes the replica to execute up to 5 billion Wasm instructions. With no per-IP throttle on `canister_query_routes` [1](#0-0) 
and no default rate-limit values [3](#0-2) 
the boundary node forwards all requests, saturating the replica's query execution threads and denying service to legitimate callers.

### Citations

**File:** rs/boundary_node/ic_boundary/src/core.rs (L856-858)
```rust
    let canister_query_routes = Router::new()
        .route(PATH_QUERY_V2, canister_handler.clone())
        .route(PATH_QUERY_V3, canister_handler.clone());
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L866-876)
```rust
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
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L996-998)
```rust
    let middleware_generic_limiter = option_layer(
        generic_limiter.map(|x| middleware::from_fn_with_state(x, generic::middleware)),
    );
```

**File:** rs/boundary_node/ic_boundary/src/core.rs (L1046-1049)
```rust
    let canister_routes = canister_query_routes
        .merge(canister_call_routes)
        .merge(canister_read_state_routes)
        .layer(canister_layers);
```

**File:** rs/boundary_node/ic_boundary/src/cli.rs (L272-277)
```rust
    #[clap(env, long, value_parser = clap::value_parser!(u32).range(1..))]
    pub rate_limit_per_second_per_subnet: Option<u32>,

    /// Allowed number of update calls per second per ip per boundary node.
    #[clap(env, long, value_parser = clap::value_parser!(u32).range(1..))]
    pub rate_limit_per_second_per_ip: Option<u32>,
```

**File:** rs/boundary_node/ic_boundary/src/cli.rs (L388-391)
```rust
pub struct Bouncer {
    /// Enable the firewall bouncer
    #[clap(env, long)]
    pub bouncer_enable: bool,
```

**File:** rs/config/src/subnet_config.rs (L39-41)
```rust
// for a single query or composite query method.
// Going above the limit results in an `InstructionLimitExceeded` error.
pub const MAX_INSTRUCTIONS_PER_QUERY_MESSAGE: NumInstructions = NumInstructions::new(5 * B);
```

**File:** rs/http_endpoints/public/src/query.rs (L189-214)
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
```
