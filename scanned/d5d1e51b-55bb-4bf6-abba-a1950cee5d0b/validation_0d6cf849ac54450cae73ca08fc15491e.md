### Title
Unauthenticated Orchestrator Dashboard Bound to All Interfaces Leaks Sensitive Node State on Port 7070 — (File: rs/orchestrator/dashboard/src/dashboard.rs)

---

### Summary

The IC Orchestrator runs an HTTP dashboard server that binds to `Ipv6Addr::UNSPECIFIED` (all interfaces) on port `7070` with no authentication. Any network-reachable party can query it and receive a plaintext dump of sensitive node internals: node ID, subnet ID, replica process PID, replica and HostOS versions, CUP state hash, firewall/IPv4 config registry versions, and the full set of authorized SSH public keys (readonly, backup, admin).

---

### Finding Description

`serve_requests()` in the `Dashboard` trait constructs the listen address as:

```rust
let addr = SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), Self::port());
``` [1](#0-0) 

`OrchestratorDashboard` sets `Self::port()` to `7070`:

```rust
const ORCHESTRATOR_DASHBOARD_PORT: u16 = 7070;
``` [2](#0-1) 

`Ipv6Addr::UNSPECIFIED` is `::` — the IPv6 wildcard — so the server accepts connections from any interface, including the node's public-facing IPv6 address. There is no TLS, no authentication token, no IP allowlist, and no rate limiting in `handle_connection()`. [3](#0-2) 

`build_response()` in `OrchestratorDashboard` serializes and returns:

```
node id, DC id, last registry version, last poll's certified time,
subnet id, replica process id, replica version, host os version,
scheduled upgrade, cup height/signed/state hash/timestamp,
firewall config registry version, ipv4 config registry version,
ssh key config registry version, ssh key configuration subnet,
readonly keys, backup keys, admin keys
``` [4](#0-3) 

The orchestrator README confirms this is intentional and documents the port:

> "The Dashboard listens for connections on port `7070` and displays the node's ID, datacenter ID, subnet ID, latest replica version, scheduled upgrades, current CUP height, registered readonly and backup keys, and more."

The test harness confirms the endpoint is reachable via the node's public IPv6 address:

```rust
let dashboard_endpoint = format!("http://[{ip}]:7070");
``` [5](#0-4) 

The `OrchestratorDashboard` is unconditionally instantiated and spawned as a task in `start_tasks()`: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

An unprivileged internet attacker who knows (or scans for) a node's public IPv6 address can send a single `GET /` HTTP request to port `7070` and receive:

- **Node ID and Subnet ID** — enables targeted attacks against specific nodes or subnets.
- **Replica process PID** — useful for process-level exploitation or fingerprinting.
- **Replica version and HostOS version** — enables version-specific exploit targeting.
- **CUP state hash and height** — leaks internal consensus state.
- **Authorized SSH public keys (readonly, backup, admin)** — reveals which keys are currently deployed, enabling an attacker to verify whether a compromised key is still active, or to enumerate authorized principals.
- **Firewall and IPv4 config registry versions** — leaks configuration state useful for timing attacks against config changes.

The combination of node ID, subnet ID, and version information provides a detailed reconnaissance profile of every IC node running the orchestrator.

---

### Likelihood Explanation

IC replica nodes have publicly routable IPv6 addresses (required for P2P and XNet communication). Port `7070` is not a well-known port and is unlikely to be blocked by upstream network policy. The endpoint requires no credentials, no TLS, and no special headers. A single HTTP GET request suffices. The attack is trivially automatable across all known IC node IPv6 addresses, which are discoverable from the public registry.

---

### Recommendation

Bind the dashboard to the loopback address only:

```rust
// Instead of:
let addr = SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), Self::port());

// Use:
let addr = SocketAddr::new(IpAddr::V6(Ipv6Addr::LOCALHOST), Self::port());
// or for IPv4 loopback:
let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), Self::port());
``` [8](#0-7) 

If remote access is operationally required, add authentication (e.g., a bearer token or mutual TLS) and restrict access via the node's firewall rules to operator IP ranges only.

---

### Proof of Concept

Given a known IC node IPv6 address `NODE_IPV6`:

```bash
curl -s http://[NODE_IPV6]:7070/
```

Expected response (no credentials required):

```
node id: <node-principal>
DC id: <datacenter-id>
last registry version: <N>
last poll's certified time: <timestamp>
subnet id: <subnet-principal>
replica process id: <PID>
replica version: <version-string>
host os version: <hostos-version>
scheduled upgrade: None
cup height: <N>
cup signed: true
cup state hash: <hex-hash>
cup timestamp: <timestamp>
firewall config registry version: <N>
ipv4 config registry version: <N>
ssh key config registry version: <N>
ssh key configuration is for subnet: <subnet-principal>
readonly keys: <ssh-public-key-1> ...
backup keys: <ssh-public-key-2> ...
admin keys: <ssh-public-key-3> ...
```

### Citations

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

**File:** rs/orchestrator/dashboard/src/dashboard.rs (L47-83)
```rust
    async fn handle_connection(&self, mut stream: TcpStream) {
        let mut buffer = [0; 512];
        if let Err(e) = stream.read(&mut buffer).await {
            self.log_info(&format!("Failed to read request: {e}"));
            return;
        }

        let get = b"GET / ";
        let response = match buffer.starts_with(get) {
            true => {
                let headers = "HTTP/1.1 200 OK\r\n\r\n";
                let contents = self.build_response();
                format!("{headers}{contents}")
            }
            false => {
                let request = match buffer.lines().next() {
                    Some(Ok(s)) => s,
                    _ => "parse error".to_string(),
                };
                let headers = "HTTP/1.1 404 NOT FOUND\r\n\r\n";
                format!(
                    "{}Not found. Only {:?} is supported, found {:?}",
                    headers,
                    std::str::from_utf8(get).expect("can't fail"),
                    request
                )
            }
        };
        stream
            .write_all(response.as_bytes())
            .await
            .unwrap_or_else(|e| self.log_info(&format!("Failed to flush stream: {e}")));
        stream
            .flush()
            .await
            .unwrap_or_else(|e| self.log_info(&format!("Failed to flush stream: {e}")));
    }
```

**File:** rs/orchestrator/src/dashboard.rs (L17-17)
```rust
const ORCHESTRATOR_DASHBOARD_PORT: u16 = 7070;
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

**File:** rs/tests/driver/src/driver/test_env_api.rs (L1908-1909)
```rust
    fn is_orchestrator_dashboard_accessible(ip: Ipv6Addr, timeout_secs: u64) -> bool {
        let dashboard_endpoint = format!("http://[{ip}]:7070");
```

**File:** rs/orchestrator/src/orchestrator.rs (L361-374)
```rust
        let orchestrator_dashboard = Some(OrchestratorDashboard::new(
            Arc::clone(&registry),
            node_id,
            ssh_access_manager.get_last_applied_parameters(),
            firewall.get_last_applied_version(),
            ipv4_configurator.get_last_applied_version(),
            registry_replicator.get_latest_certified_time(),
            replica_process,
            Arc::clone(&subnet_assignment),
            replica_version,
            hostos_version.ok(),
            local_cup_reader,
            logger.clone(),
        ));
```

**File:** rs/orchestrator/src/orchestrator.rs (L643-648)
```rust
        if let Some(dashboard) = self.orchestrator_dashboard.take() {
            self.task_tracker.spawn(
                "dashboard",
                serve_dashboard(dashboard, cancellation_token.clone()),
            );
        }
```
