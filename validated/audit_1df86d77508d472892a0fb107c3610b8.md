Audit Report

## Title
Boundary Node Health Checker Marks Nodes Unhealthy on Single Transient 503 with No Retry — (`rs/boundary_node/ic_boundary/src/check.rs`)

## Summary
`Checker::check` treats any non-200 HTTP response, including a 503 emitted by the replica's load-shed layer, as an immediate, unretried node health failure. Because the replica's `/api/v2/status` endpoint has a hard concurrency cap of 100 slots backed by a 20-minute connection timeout and 1000-stream HTTP/2 multiplexing, an unprivileged attacker can saturate those slots with a single HTTP/2 connection using zero-window slow-read streams, causing every subsequent health-check request to receive 503. The boundary node then removes every affected node from routing, making the entire subnet unreachable through the boundary node despite all replicas being fully operational.

## Finding Description

**Concurrency cap on `/api/v2/status`:**
The status router is wrapped with `.load_shed()` followed by `GlobalConcurrencyLimitLayer::new(config.max_status_concurrent_requests)`. [1](#0-0) 
The default cap is 100 concurrent requests. [2](#0-1) 
The HTTP/2 connection timeout is 1200 seconds and the per-connection stream limit is 1000, meaning a single connection can hold all 100 slots open for up to 20 minutes. [3](#0-2) 

**No tolerance for non-200 in `Checker::check`:**
Any response with a status code other than 200 — including 503 from the load-shed layer — is immediately returned as `CheckError::Http`. [4](#0-3) 
There is no retry, no 503-specific branch, and no distinction between a transient overload signal and a genuine node failure.

**Single failure immediately sets `healthy = false`:**
`NodeActor::check` maps any `Err(_)` from `Checker::check` directly to `healthy = false` with no consecutive-failure counter. [5](#0-4) 
The `CHECKS_MSG_PERIODICITY` counter only gates height/latency updates; the health field is unconditionally overwritten on every check cycle. [6](#0-5) 

**Immediate state propagation to SubnetActor:**
Whenever `new_state != self.state`, the updated `NodeState` (with `healthy: false`) is sent to `SubnetActor` immediately, removing the node from the healthy routing set. [7](#0-6) 

**Exploit flow:**
1. Attacker opens one HTTP/2 connection to `https://<replica_ip>:8080`.
2. Sends 100 `GET /api/v2/status` requests with `WINDOW_UPDATE = 0` (zero receive window), preventing the server from flushing responses and holding each request in the concurrency counter.
3. The 101st request — the boundary node's periodic health check — receives HTTP 503 from the load-shed layer.
4. `Checker::check` returns `Err(CheckError::Http(503))`.
5. `NodeActor` sets `healthy = false` and notifies `SubnetActor`.
6. Repeated for all N nodes in a subnet, the boundary node routes zero traffic to that subnet.

## Impact Explanation
The attack causes the boundary node to stop routing all client traffic to a targeted subnet while the subnet itself remains fully operational. This is a platform-level availability impact — subnet unreachability through the boundary node — matching the High bounty impact: *"Application/platform-level DoS… or subnet availability impact not based on raw volumetric DDoS."* The attack is not volumetric; it exploits HTTP/2 stream multiplexing and a specific design gap in the health checker to achieve targeted concurrency exhaustion with a single connection per replica.

## Likelihood Explanation
The replica's public HTTP endpoint is accessible by design to all users. No credentials, no privileged network position, and no protocol compromise are required. One HTTP/2 connection per target replica is sufficient. The 20-minute connection timeout means the attacker needs to maintain only idle streams. The 100-slot default limit is not a meaningful barrier given HTTP/2 multiplexing. The absence of any retry or transient-error tolerance means the attack succeeds on the first health-check cycle that coincides with saturation, and the node remains marked unhealthy for as long as the attacker holds the streams open.

## Recommendation
1. **`Checker::check`**: Introduce a transient-error variant (e.g., `CheckError::Transient`) for 503, 429, 502, and 504 responses. Do not return `CheckError::Http` for these codes; instead return the transient variant so `NodeActor` does not count them as health failures.
2. **`NodeActor`**: Require N consecutive failures (e.g., 3) before setting `healthy = false`, to tolerate transient load spikes. A single success should reset the counter.
3. **Replica**: Consider a separate, dedicated concurrency pool for `/api/v2/status` that is not shared with public traffic, or exempt known boundary-node source IPs from the `GlobalConcurrencyLimitLayer` for the status endpoint.

## Proof of Concept
```
# For each replica node in the target subnet:
# 1. Open one HTTP/2 connection to https://<node_ip>:8080
# 2. Send 100 GET /api/v2/status with SETTINGS_INITIAL_WINDOW_SIZE=0
#    (server cannot flush response body; slot held in concurrency counter)
# 3. Confirm: 101st GET /api/v2/status returns HTTP 503
# 4. Wait for BN health-check interval; observe node marked unhealthy in BN logs
# 5. Repeat for all N nodes in subnet
# 6. Verify: boundary node routes 0 requests to subnet (all nodes healthy=false)
# 7. Release streams; verify recovery on next health-check cycle
#
# Deterministic integration test:
# - Spin up a replica with max_status_concurrent_requests=2 (for speed)
# - Open HTTP/2 connection, send 2 zero-window GET /api/v2/status
# - Invoke Checker::check against the replica; assert Err(CheckError::Http(503))
# - Assert NodeActor emits NodeState { healthy: false }
```

### Citations

**File:** rs/http_endpoints/public/src/lib.rs (L591-602)
```rust
    let service_builder = |concurrency_limit_layer: GlobalConcurrencyLimitLayer| {
        ServiceBuilder::new()
            .layer(HandleErrorLayer::new(map_box_error_to_response))
            .load_shed()
            .layer(concurrency_limit_layer)
    };

    let final_router =
        base_router
            .merge(http_handler.status_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_status_concurrent_requests),
            )))
```

**File:** rs/config/src/http_handler.rs (L76-78)
```rust
            connection_read_timeout_seconds: 1_200, // 20 min
            request_timeout_seconds: 300,           // 5 min
            http_max_concurrent_streams: 1000,
```

**File:** rs/config/src/http_handler.rs (L84-84)
```rust
            max_status_concurrent_requests: 100,
```

**File:** rs/boundary_node/ic_boundary/src/check.rs (L122-133)
```rust
        let (healthy, height, latency_change) = match &res {
            Ok(res) => {
                let latency = start.elapsed().as_micros() as u64;
                let current_avg = self.avg_mov_latency.get_average();
                self.avg_mov_latency.add_sample(latency);
                let latency_change = (latency.abs_diff(current_avg) as f64) / (current_avg as f64);
                (true, res.height, latency_change)
            }

            // Note: we don't add latency to the moving average in case of an error.
            Err(_) => (false, 0, 0.0),
        };
```

**File:** rs/boundary_node/ic_boundary/src/check.rs (L141-141)
```rust
        new_state.healthy = healthy;
```

**File:** rs/boundary_node/ic_boundary/src/check.rs (L157-163)
```rust
        if Some(new_state) != self.state {
            debug!("{self}: new state: {new_state:?}");

            self.state = Some(new_state);
            // It can never fail in our case
            let _ = self.channel.send((self.idx, new_state)).await;
        }
```

**File:** rs/boundary_node/ic_boundary/src/check.rs (L717-719)
```rust
        if response.status() != reqwest::StatusCode::OK {
            return Err(CheckError::Http(response.status().into()));
        }
```
