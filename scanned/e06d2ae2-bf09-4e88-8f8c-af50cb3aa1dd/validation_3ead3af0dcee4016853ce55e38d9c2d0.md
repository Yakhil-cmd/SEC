### Title
Unauthenticated Information Disclosure of Canister Controller Principals and Cycles Balances via `GET /_/dashboard` — (`rs/http_endpoints/public/src/dashboard.rs`)

---

### Summary

The `/_/dashboard` HTTP endpoint on every IC replica node exposes the full set of `(canister_id, controller_principals, cycles_balance)` tuples for every canister on the subnet to any unauthenticated HTTP client. No authentication, IP restriction, or authorization check exists anywhere in the handler or its router registration.

---

### Finding Description

The `dashboard` async handler in `rs/http_endpoints/public/src/dashboard.rs` reads the latest replicated state and passes it directly to the Askama template: [1](#0-0) 

The handler has no authentication guard, no IP allowlist, and no middleware beyond a concurrency limiter. It is registered into the public router with only `GlobalConcurrencyLimitLayer`: [2](#0-1) 

The Askama template at `rs/http_endpoints/public/templates/dashboard.html` renders, for every canister on the subnet:

- **Line 101**: `c.system_state.collect_controllers_as_string()` — all controller principal IDs
- **Line 132**: `c.system_state.balance()` — exact cycles balance
- **Line 97**: `c.canister_id()` — canister ID [3](#0-2) 

The `collect_controllers_as_string` method is defined in: [4](#0-3) 

The final router assembly confirms no auth layer is applied to `dashboard_router`: [5](#0-4) 

---

### Impact Explanation

The **cycles balance** of a canister is normally only accessible to its controllers via the `canister_status` management canister call — it is not a public datum. This endpoint bypasses that access control entirely, exposing the exact cycles balance of every canister on the subnet to any unauthenticated requester.

Controller principal IDs are also exposed. While `canister_info` makes controllers queryable, the dashboard aggregates the full subnet-wide mapping in a single unauthenticated response, enabling bulk enumeration.

The concrete impact is **information disclosure**: an attacker learns which canisters hold large cycles balances and who controls them, without any authentication.

The question's further claim — that this enables credential-stuffing or social engineering to *drain* cycles — is speculative and requires additional attacker capabilities (compromising a controller's private key). That secondary step is out of scope as a direct exploit. The root vulnerability is the unauthenticated disclosure itself.

---

### Likelihood Explanation

The `rs/http_endpoints/public` package is the replica's public-facing HTTP server. Replica node IPs are publicly discoverable from the IC registry. Whether port 8080 is directly reachable from the internet depends on network-level firewall rules outside this codebase. At minimum, boundary nodes connect to this endpoint, and the `/_/` path prefix is known to be proxied (as evidenced by PocketIC's HTTP gateway explicitly forwarding `/_/dashboard`). The code contains zero mitigations.

---

### Recommendation

1. Restrict `/_/dashboard` to loopback/localhost only at the network binding level, or
2. Add an IP allowlist middleware (operator/monitoring IPs only) to the `dashboard_router`, or
3. Remove cycles balance and controller principal data from the template, replacing them with non-sensitive operational metrics only.

---

### Proof of Concept

```bash
# Assuming direct access to a replica node's public HTTP port
curl -s http://<replica-node-ip>:8080/_/dashboard | grep -E "controllers|Cycles balance"
```

The response HTML will contain, for every canister on the subnet, its controller principal strings (line 101 of the template) and exact cycles balance (line 132 of the template), with no `Authorization` header required. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/http_endpoints/public/src/dashboard.rs (L56-60)
```rust
        Router::new().route(
            DashboardService::route(),
            axum::routing::get(dashboard).with_state(state),
        )
    }
```

**File:** rs/http_endpoints/public/src/dashboard.rs (L63-99)
```rust
async fn dashboard(
    State(DashboardService {
        config,
        subnet_type,
        state_reader,
    }): State<DashboardService>,
) -> impl IntoResponse {
    let labeled_state =
        match tokio::task::spawn_blocking(move || state_reader.get_latest_state()).await {
            Ok(labeled_state) => labeled_state,
            Err(err) => {
                return (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("Internal Error: {err}"),
                )
                    .into_response();
            }
        };

    // See https://github.com/djc/askama/issues/333
    let state = labeled_state.get_ref();
    let canisters: Vec<(
        &ic_replicated_state::CanisterState,
        &ic_replicated_state::CanisterPriority,
    )> = state
        .canisters_iter()
        .map(|canister| (canister, state.canister_priority(&canister.canister_id())))
        .collect();

    let dashboard = Dashboard {
        subnet_type,
        http_config: &config,
        height: labeled_state.height(),
        replicated_state: labeled_state.get_ref(),
        canisters: &canisters,
        replica_version: ReplicaVersion::default(),
    };
```

**File:** rs/http_endpoints/public/src/lib.rs (L598-694)
```rust
    let final_router =
        base_router
            .merge(http_handler.status_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_status_concurrent_requests),
            )))
            .merge(http_handler.call_v2_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_call_concurrent_requests),
            )))
            // TODO(CON-1574): see if there is any reasonable explicit concurrency limit we could use here.
            .merge(http_handler.call_v3_router)
            .merge(http_handler.call_v4_router)
            .merge(http_handler.subnet_call_v4_router)
            .merge(http_handler.query_v2_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_query_concurrent_requests),
            )))
            .merge(http_handler.query_v3_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_query_concurrent_requests),
            )))
            .merge(http_handler.subnet_query_v3_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_query_concurrent_requests),
            )))
            .merge(
                http_handler
                    .subnet_read_state_v2_router
                    .layer(service_builder(GlobalConcurrencyLimitLayer::new(
                        config.max_read_state_concurrent_requests,
                    ))),
            )
            .merge(
                http_handler
                    .subnet_read_state_v3_router
                    .layer(service_builder(GlobalConcurrencyLimitLayer::new(
                        config.max_read_state_concurrent_requests,
                    ))),
            )
            .merge(
                http_handler
                    .canister_read_state_v2_router
                    .layer(service_builder(GlobalConcurrencyLimitLayer::new(
                        config.max_read_state_concurrent_requests,
                    ))),
            )
            .merge(
                http_handler
                    .canister_read_state_v3_router
                    .layer(service_builder(GlobalConcurrencyLimitLayer::new(
                        config.max_read_state_concurrent_requests,
                    ))),
            )
            .merge(http_handler.catchup_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_catch_up_package_concurrent_requests),
            )))
            .merge(http_handler.dashboard_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_dashboard_concurrent_requests),
            )))
            .merge(
                http_handler
                    .pprof_home_router
                    .layer(service_builder(pprof_concurrency_limiter.clone())),
            )
            .merge(
                http_handler
                    .pprof_flamegraph_router
                    .layer(service_builder(pprof_concurrency_limiter.clone())),
            )
            .merge(
                http_handler
                    .pprof_profile_router
                    .layer(service_builder(pprof_concurrency_limiter)),
            )
            .merge(
                http_handler
                    .tracing_flamegraph_router
                    .layer(service_builder(GlobalConcurrencyLimitLayer::new(
                        config.max_tracing_flamegraph_concurrent_requests,
                    ))),
            );

    final_router.layer(
        ServiceBuilder::new()
            .layer(TraceLayer::new_for_http())
            .layer(HandleErrorLayer::new(map_box_error_to_response))
            .layer(health_status_refresher.clone())
            .load_shed()
            .timeout(Duration::from_secs(config.request_timeout_seconds))
            .layer(axum::middleware::from_fn_with_state(
                Arc::new(metrics),
                collect_timer_metric,
            ))
            // Disable default limit since apply a request limit to all routes.
            .layer(DefaultBodyLimit::disable())
            .layer(RequestBodyLimitLayer::new(
                config.max_request_size_bytes as usize,
            ))
            .layer(cors_layer()),
    )
}
```

**File:** rs/http_endpoints/public/templates/dashboard.html (L93-133)
```html
    {% for (c, cp) in canisters %}
    <tr>
        <td class="text">
            <details>
                <summary>{{ c.canister_id() }}</summary>
                <div class="verbose">
                    <h3>System state</h3>
                    <table>
                        <tr><td>controllers</td><td>{{ c.system_state.collect_controllers_as_string() }}</td></tr>
                        <tr><td>certified_data length</td><td>{{ c.system_state.certified_data.len() }} bytes</td></tr>
                        <tr><td>canister_history_memory_usage</td><td>{{ c.system_state.canister_history_memory_usage() }} bytes</td></tr>
                    </table>
                    <h3>Execution state</h3>
                    {% match c.execution_state.as_ref() %}
                    {% when Some with (exec_state) %}
                    <table>
                        <tr><td>wasm_binary size</td><td>{{ exec_state.wasm_binary.binary.len() }} bytes</td></tr>
                        <tr><td>wasm_binary sha256</td><td>{{ hex::encode(exec_state.wasm_binary.binary.module_hash()) }}</td></tr>
                        <tr><td>heap_size</td><td>{{ exec_state.wasm_memory.size }} pages</td></tr>
                        <tr><td>stable_memory_size</td><td>{{ exec_state.stable_memory.size }} pages</td></tr>
                        <tr><td>exports</td><td>
                            {% let dbg = format!("{:?}",  exec_state.exports) %}
                            {% if dbg.len() > 25000 %}
                              Exports' debug string is too large: {{  dbg.len() }} bytes!
                            {% else %}
                              {{ dbg }}
                            {% endif %}
                        </td></tr>
                    </table>
                    {% when None %}
                    <div>No execution state</div>
                    {% endmatch %}
                    <h3>Scheduler state</h3>
                    <table>
                        <tr><td>last_full_execution_round</td><td>{{ cp.last_full_execution_round }}</td></tr>
                        <tr><td>compute_allocation</td><td>{{ c.compute_allocation() }}</td></tr>
                        <tr><td>freeze_threshold (seconds)</td><td>{{ c.system_state.freeze_threshold }}</td></tr>
                        <tr><td>memory_usage</td><td>{{ c.memory_usage() }}</td></tr>
                        <tr><td>accumulated_priority</td><td>{{ cp.accumulated_priority.get() }} </td></tr>
                        <tr><td>Cycles balance</td><td>{{ c.system_state.balance() }}</td></tr>
                    </table>
```

**File:** rs/replicated_state/src/canister_state/system_state.rs (L1-1)
```rust
mod call_context_manager;
```
