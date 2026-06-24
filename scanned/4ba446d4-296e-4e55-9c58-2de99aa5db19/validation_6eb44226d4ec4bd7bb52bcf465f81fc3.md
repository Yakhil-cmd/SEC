### Title
Boundary Node Health Checker Treats Transient 503 (Load-Shed) as Permanent Node Failure, Enabling Availability Outage via Status Endpoint Concurrency Exhaustion — (`rs/boundary_node/ic_boundary/src/check.rs`, `rs/http_endpoints/public/src/lib.rs`)

---

### Summary

An unprivileged attacker who can reach a replica's public HTTP endpoint can exhaust the `/api/v2/status` concurrency limit using HTTP/2 slow-read streams. The boundary node health checker (`Checker::check`) treats any non-200 response — including a 503 produced by the load-shed layer — as an immediate, unretried node health failure. A single such 503 marks the node unhealthy and removes it from routing. By repeating this for every node in a subnet, the attacker can cause the boundary node to stop routing all traffic to that subnet.

---

### Finding Description

**Entrypoint — status endpoint concurrency limit:**

The status router is wrapped with `.load_shed()` followed by `GlobalConcurrencyLimitLayer`. When `max_status_concurrent_requests` in-flight requests are active, the load-shed layer immediately returns HTTP 503 to any additional request. [1](#0-0) 

The default value is 100: [2](#0-1) 

**Attacker technique — HTTP/2 slow-read stream exhaustion:**

HTTP/2 allows a client to set its receive-window to zero, preventing the server from sending the response body. The server holds the request in the concurrency counter until the connection times out (`connection_read_timeout_seconds = 1200s`). A single HTTP/2 connection can hold up to `http_max_concurrent_streams = 1000` streams, so one connection is sufficient to saturate the 100-slot limit.

**Boundary node checker — no retry, no 503 tolerance:**

`Checker::check` makes a direct GET to `https://{node_principal_id}:{port}/api/v2/status`: [3](#0-2) 

Any non-200 response — including 503 — is immediately returned as `CheckError::Http`: [4](#0-3) 

There is no retry, no consecutive-failure threshold, and no distinction between "overloaded" and "actually failed." Any error sets `healthy = false` immediately: [5](#0-4) 

`NodeActor` propagates this state change immediately to `SubnetActor`, which removes the node from the healthy set and stops routing to it: [6](#0-5) 

---

### Impact Explanation

For each replica in a subnet, the attacker holds 100 HTTP/2 streams open against `/api/v2/status`. The boundary node's periodic health check receives 503 and marks the node unhealthy. Repeating for all nodes in a subnet causes the boundary node to route zero traffic to that subnet. The subnet itself is unaffected — replicas are healthy — but all client traffic routed through the boundary node is dropped. Recovery occurs only when the attacker releases connections and the next health check cycle succeeds.

---

### Likelihood Explanation

The replica HTTP endpoint is publicly accessible by design (users submit transactions directly to replicas). The attack requires no credentials, no privileged network position, and no protocol-level compromise. A single machine with one HTTP/2 connection per target replica is sufficient. The default limit of 100 is not a meaningful barrier given HTTP/2 stream multiplexing. The absence of any retry or transient-error tolerance in `Checker::check` means the attack succeeds on the very first health check cycle that coincides with saturation.

---

### Recommendation

1. **In `Checker::check`**: Distinguish 503 (and other transient codes such as 429, 502, 504) from genuine health failures. Do not return `CheckError::Http` for these; instead return a transient error variant that `NodeActor` does not count as a health failure.
2. **In `NodeActor`**: Require N consecutive failures (e.g., 3) before marking a node unhealthy, to tolerate transient load spikes.
3. **On the replica**: Consider exempting health-check requests (e.g., by source IP allowlist for known boundary nodes) from the `GlobalConcurrencyLimitLayer`, or use a separate, dedicated concurrency pool for `/api/v2/status` that is not shared with public traffic.

---

### Proof of Concept

```
# For each replica node in the target subnet:
# 1. Open one HTTP/2 connection to https://<node_ip>:8080
# 2. Send 100 GET /api/v2/status requests with WINDOW_UPDATE=0
#    (prevents server from flushing response; holds slot in concurrency counter)
# 3. The 101st request (from the BN checker) receives HTTP 503
# 4. Checker::check returns Err(CheckError::Http(503))
# 5. NodeActor sets healthy=false, SubnetActor removes node from routing
# 6. Repeat for all N nodes in subnet
# 7. Boundary node routes 0 requests to subnet; subnet is effectively unreachable
#    through the boundary node despite all replicas being fully operational
``` [7](#0-6)

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

**File:** rs/config/src/http_handler.rs (L84-84)
```rust
            max_status_concurrent_requests: 100,
```

**File:** rs/boundary_node/ic_boundary/src/snapshot.rs (L78-79)
```rust
        let health_check_url = Url::from_str(&format!("https://{id}:{port}/api/v2/status"))
            .context("unable to create health check URL")?;
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

**File:** rs/boundary_node/ic_boundary/src/check.rs (L157-163)
```rust
        if Some(new_state) != self.state {
            debug!("{self}: new state: {new_state:?}");

            self.state = Some(new_state);
            // It can never fail in our case
            let _ = self.channel.send((self.idx, new_state)).await;
        }
```

**File:** rs/boundary_node/ic_boundary/src/check.rs (L706-748)
```rust
    async fn check(&self, node: &Node) -> Result<CheckResult, CheckError> {
        let mut request = reqwest::Request::new(Method::GET, node.health_check_url.clone());
        *request.timeout_mut() = Some(self.timeout);

        // Execute request
        let response = self
            .http_client
            .execute(request)
            .await
            .map_err(|err| CheckError::Network(err.to_string()))?;

        if response.status() != reqwest::StatusCode::OK {
            return Err(CheckError::Http(response.status().into()));
        }

        let response_reader = match response.bytes().await {
            Ok(v) => v.reader(),
            Err(e) => return Err(CheckError::ReadBody(e.to_string())),
        };

        let HttpStatusResponse {
            replica_health_status,
            certified_height,
            impl_version,
            ..
        } = match serde_cbor::from_reader(response_reader) {
            Ok(v) => v,
            Err(e) => return Err(CheckError::Cbor(e.to_string())),
        };

        if replica_health_status != Some(ReplicaHealthStatus::Healthy) {
            return Err(CheckError::Health);
        }

        if impl_version.is_none() {
            return Err(CheckError::Generic("No replica version available".into()));
        }

        Ok(CheckResult {
            height: certified_height.map_or(0, |v| v.get()),
            replica_version: impl_version.unwrap(),
        })
    }
```
