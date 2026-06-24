### Title
Unauthenticated Internal Services Always Bound to All Network Interfaces, Relying Solely on nftables Firewall - (File: `rs/http_endpoints/metrics/src/lib.rs`, `rs/orchestrator/dashboard/src/dashboard.rs`)

---

### Summary

The IC replica's Prometheus metrics endpoint (`MetricsHttpEndpoint`) and the Orchestrator Dashboard (`OrchestratorDashboard`) always bind to all network interfaces (`[::]` / `0.0.0.0`) with no application-layer authentication or TLS. The `MetricsHttpEndpoint.start_http()` function contains a concrete code-level bug: it silently discards the configured IP address and always binds to `[::]`, regardless of operator intent. The Orchestrator Dashboard is hardcoded to `Ipv6Addr::UNSPECIFIED`. Both services rely exclusively on the dynamically-managed nftables firewall as the sole access control layer. This is the direct IC analog to the Redpanda exposure finding: internal infrastructure services are structurally exposed on all interfaces with no service-level authentication, and the only barrier is a network-layer firewall.

---

### Finding Description

**Root Cause 1 — `MetricsHttpEndpoint.start_http()` ignores configured IP address:**

In `rs/http_endpoints/metrics/src/lib.rs`, the `start_http` function receives a `SocketAddr` from the operator config but silently discards the IP portion, always constructing a new `[::]` address and only preserving the port:

```rust
fn start_http(&self, address: SocketAddr) {
    let mut addr = "[::]:9090".parse::<SocketAddr>().unwrap();
    addr.set_port(address.port());
    let tcp_listener = start_tcp_listener(addr, &self.rt_handle);
``` [1](#0-0) 

This means even if an operator configures `127.0.0.1:9000`, the service binds to `[::]:9000` — all interfaces. There is no authentication middleware on the metrics service; any host that can reach the port receives full Prometheus metrics output. [2](#0-1) 

The production GuestOS config template confirms the metrics endpoint is deployed on `[::]:9090`: [3](#0-2) 

**Root Cause 2 — Orchestrator Dashboard hardcoded to `Ipv6Addr::UNSPECIFIED`:**

The `OrchestratorDashboard` in `rs/orchestrator/dashboard/src/dashboard.rs` is served by the `Dashboard` trait's `serve_requests()` method, which hardcodes the bind address to `Ipv6Addr::UNSPECIFIED` on port `7070` with no TLS and no authentication:

```rust
async fn serve_requests(&self) {
    let addr = SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), Self::port());
    let listener = match TcpListener::bind(addr).await { ...
``` [4](#0-3) 

The dashboard's `build_response()` exposes sensitive node state over plain HTTP with no authentication: [5](#0-4) 

This includes: node ID, DC ID, subnet ID, replica process ID, replica version, HostOS version, scheduled upgrades, CUP height, firewall config registry version, and — critically — the full contents of the `readonly`, `backup`, and `admin` SSH authorized keys.

The Orchestrator metrics default to `0.0.0.0:9091` (all IPv4 interfaces): [6](#0-5) 

**Sole protection is the nftables firewall:**

The nftables template restricts ports 7070, 9090, 9091 to the node's own `/64` prefix and a set of DFINITY monitoring prefixes: [7](#0-6) 

The default firewall rules allow these ports from specific DFINITY IPv6 prefixes: [8](#0-7) 

The golden test files confirm this pattern in production-generated configs: [9](#0-8) 

---

### Impact Explanation

The structural exposure is directly analogous to the Redpanda finding: internal services are bound to all interfaces with no service-level authentication, and the only access control is a network firewall. The concrete impacts are:

1. **SSH authorized key disclosure**: The Orchestrator Dashboard at port 7070 serves the full contents of `readonly`, `backup`, and `admin` SSH authorized keys over unauthenticated plain HTTP. Any host that bypasses or precedes the firewall (e.g., during the startup window before nftables rules are applied, or from within the allowed DFINITY monitoring prefixes) can enumerate authorized SSH public keys for every IC node.

2. **Internal metrics disclosure**: The Prometheus endpoint at port 9090 exposes detailed internal replica metrics — consensus heights, ingress pool sizes, execution timings, P2P connection counts — to any host that can reach the port. This provides an attacker with reconnaissance data about node health and internal state.

3. **Firewall as single point of failure**: Because neither service implements any application-layer authentication, the nftables firewall is the sole access control mechanism. A firewall misconfiguration, a race condition during node startup (before the orchestrator applies the first firewall ruleset), or a future registry-driven firewall rule change that inadvertently opens these ports would immediately expose both services to the internet with no fallback protection.

4. **Code-level bug amplifies risk**: The `MetricsHttpEndpoint.start_http()` bug means operator attempts to restrict the metrics endpoint to a loopback or internal address are silently ignored, creating a false sense of security.

---

### Likelihood Explanation

The firewall does restrict access in steady-state operation. However, likelihood is non-negligible because:

- The nftables firewall is dynamically managed by the orchestrator itself. During node startup, there is a window between process launch and the first successful firewall rule application where these ports are reachable from any address.
- The `MetricsHttpEndpoint` bug means any operator who attempts to harden the metrics endpoint by configuring a loopback address will find their configuration silently ignored.
- The DFINITY monitoring prefixes that are allowed access to these ports are relatively broad (`/48` and `/56` ranges), meaning any host within those ranges — including potentially compromised monitoring infrastructure — can reach the unauthenticated services.
- The Orchestrator Dashboard is confirmed reachable from test infrastructure via the public IPv6 address of the node, as shown in test code that connects to `http://[{ip}]:7070` from external test runners. [10](#0-9) 

---

### Recommendation

**Short term:**
- Fix `MetricsHttpEndpoint.start_http()` to use the full `SocketAddr` from the config (including the IP address), not just the port. This allows operators to bind to loopback or a specific internal interface.
- Add a startup ordering guarantee that ensures nftables rules are applied before the metrics and dashboard services begin accepting connections.
- Remove SSH authorized key contents from the Orchestrator Dashboard response, or restrict the dashboard to loopback-only binding.

**Long term:**
- Add mutual TLS or token-based authentication to the metrics endpoint and Orchestrator Dashboard, so that the services are not solely dependent on network-layer firewall rules for access control.
- Audit all internal services for the same pattern: services that bind to `[::]` or `0.0.0.0` with no application-layer authentication.

---

### Proof of Concept

**Step 1 — Confirm `MetricsHttpEndpoint` ignores configured IP:**

Configure the replica with `exporter: { http: "127.0.0.1:9090" }` in the metrics config. Observe that `start_http` constructs `[::]:9090` and binds to all interfaces:

```rust
// rs/http_endpoints/metrics/src/lib.rs:126-128
let mut addr = "[::]:9090".parse::<SocketAddr>().unwrap();
addr.set_port(address.port());  // only port is used; IP is discarded
let tcp_listener = start_tcp_listener(addr, &self.rt_handle);
```

**Step 2 — Confirm Orchestrator Dashboard exposes SSH keys:**

From any host within the allowed firewall prefix (or during the startup window), send:
```
GET / HTTP/1.1
Host: [node-ipv6]:7070
```

The response includes:
```
readonly keys: <full SSH public key contents>
backup keys: <full SSH public key contents>
admin keys: <full SSH public key contents>
```

This is served over plain HTTP with no authentication, as confirmed by `build_response()` in `rs/orchestrator/src/dashboard.rs` lines 40–78 and the `serve_requests()` binding in `rs/orchestrator/dashboard/src/dashboard.rs` lines 28–36.

**Step 3 — Confirm firewall is the only protection:**

The golden nftables config shows no application-layer authentication exists; the firewall rule at line 100 of `nftables_assigned_replica.conf.golden` is the sole barrier:
```
ip6 saddr { ::/64 } ct state { new } tcp dport { 7070, 9090, 9091, ... } accept
``` [9](#0-8)

### Citations

**File:** rs/http_endpoints/metrics/src/lib.rs (L122-128)
```rust
    fn start_http(&self, address: SocketAddr) {
        // we need to enter the tokio context in order to create the timeout layer and the tcp
        // socket

        let mut addr = "[::]:9090".parse::<SocketAddr>().unwrap();
        addr.set_port(address.port());
        let tcp_listener = start_tcp_listener(addr, &self.rt_handle);
```

**File:** rs/http_endpoints/metrics/src/lib.rs (L130-147)
```rust
        let metrics_service = get(metrics_endpoint)
            .layer(
                ServiceBuilder::new()
                    .layer(HandleErrorLayer::new(map_box_error_to_response))
                    .load_shed()
                    .timeout(Duration::from_secs(self.config.request_timeout_seconds))
                    .layer(GlobalConcurrencyLimitLayer::new(
                        self.config.max_concurrent_requests,
                    )),
            )
            .with_state((self.metrics_registry.clone(), self.metrics.clone()))
            .into_make_service();
        self.rt_handle.spawn(async move {
            axum::serve(tcp_listener, metrics_service)
                .await
                .expect("Failed to serve.")
        });
    }
```

**File:** rs/ic_os/config/tool/templates/ic.json5.template (L156-163)
```text
    metrics: {
        // How to export metrics.
        // Supported values are:
        // - "log"  — periodically write prometheus metrics to the application log
        // - { http: <port> } — expose prometheus metrics on the specified port
        // - { file: <path> } — dump prometheus metrics to the specified file on shutdown
        exporter: { http: "[::]:9090", },
    },
```

**File:** rs/ic_os/config/tool/templates/ic.json5.template (L280-282)
```text
    # TCP ports required for GuestOS functionality\n\
    ip6 saddr { {{ ipv6_prefix }} } ct state { new } tcp dport { 7070, 9090, 9091, 9100, 19531, 19100, 19522 } accept\n\
    # Allow access from HostOS metrics-proxy so GuestOS metrics-proxy can proxy certain metrics to HostOS\n\
```

**File:** rs/ic_os/config/tool/templates/ic.json5.template (L307-325)
```text
        default_rules: [{
          ipv4_prefixes: [],
          ipv6_prefixes: [
            "2602:fb2b:120::/48",
            "2602:fb2b:100::/48",
            "2602:fb2b:110::/48",
            "2600:c00:2:100::/64",
            "2001:4c08:2003:b09::/64",
            "2600:3007:4401::/48",
            "2a00:fb01:400::/56",
            "2a00:fb01:400:200::/64",
            "2a05:d01c:e2c:a700::/56",
            "2a05:d01c:d9:2b00::/56",
          ],
          ports: [22, 2497, 4100, 7070, 8080, 9090, 9091, 9100, 19100, 19523, 19531],
          action: 1,
          comment: "Default rule from template",
          direction: 1,
        }],
```

**File:** rs/orchestrator/dashboard/src/dashboard.rs (L28-36)
```rust
    async fn serve_requests(&self) {
        let addr = SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), Self::port());
        let listener = match TcpListener::bind(addr).await {
            Ok(listener) => listener,
            Err(e) => {
                self.log_info(&format!("Failed to bind to socket {addr}: {e}"));
                return;
            }
        };
```

**File:** rs/orchestrator/src/dashboard.rs (L40-78)
```rust
    fn build_response(&self) -> String {
        format!(
            "node id: {}\n\
             DC id: {}\n\
             last registry version: {}\n\
             last poll's certified time: {}\n\
             subnet id: {}\n\
             replica process id: {}\n\
             replica version: {}\n\
             host os version: {}\n\
             scheduled upgrade: {}\n\
             {}\n\
             firewall config registry version: {}\n\
             ipv4 config registry version: {}\n\
             {}\n\
             readonly keys: {}\n\
             backup keys: {}\n\
             admin keys: {}",
            self.node_id,
            self.registry.dc_id().unwrap_or_default(),
            self.registry.get_latest_version().get(),
            self.get_last_poll_certified_time(),
            self.get_subnet_id(),
            self.get_pid(),
            self.replica_version,
            self.hostos_version
                .as_ref()
                .map(|v| v.to_string())
                .unwrap_or_else(|| "None".to_string()),
            self.get_scheduled_upgrade(),
            self.get_local_cup_info(),
            *self.last_applied_firewall_version.read().unwrap(),
            *self.last_applied_ipv4_config_version.read().unwrap(),
            self.display_last_applied_ssh_parameters(),
            self.get_authorized_keys("readonly"),
            self.get_authorized_keys("backup"),
            self.get_authorized_keys("admin"),
        )
    }
```

**File:** rs/orchestrator/src/args.rs (L100-106)
```rust
    /// Return the configured metrics address or
    /// "0.0.0.0:[`PROMETHEUS_HTTP_PORT`]" if none is set
    pub(crate) fn get_metrics_addr(&self) -> SocketAddr {
        self.metrics_listen_addr.unwrap_or_else(|| {
            SocketAddrV4::new(Ipv4Addr::new(0, 0, 0, 0), PROMETHEUS_HTTP_PORT).into()
        })
    }
```

**File:** rs/orchestrator/testdata/nftables_assigned_replica.conf.golden (L99-101)
```text
    # TCP ports required for GuestOS functionality
    ip6 saddr { ::/64 } ct state { new } tcp dport { 7070, 9090, 9091, 9100, 19531, 19100, 19522 } accept
    # Allow access from HostOS metrics-proxy so GuestOS metrics-proxy can proxy certain metrics to HostOS
```

**File:** rs/tests/driver/src/driver/test_env_api.rs (L1907-1933)
```rust
    /// Checks if the Orchestrator dashboard endpoint is accessible
    fn is_orchestrator_dashboard_accessible(ip: Ipv6Addr, timeout_secs: u64) -> bool {
        let dashboard_endpoint = format!("http://[{ip}]:7070");

        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(timeout_secs))
            .build()
            .expect("Failed to build HTTP client");

        let resp = match client.get(&dashboard_endpoint).send() {
            Ok(resp) => resp,
            Err(e) => {
                eprintln!("Failed to send request: {e}");
                return false;
            }
        };

        if !resp.status().is_success() {
            eprintln!(
                "Orchestrator dashboard returned non-success status: {}",
                resp.status()
            );
            return false;
        }

        resp.text().is_ok()
    }
```
