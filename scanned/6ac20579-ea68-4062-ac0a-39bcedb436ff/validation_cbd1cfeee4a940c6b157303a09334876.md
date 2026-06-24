The code confirms the vulnerability. Let me trace it precisely.

**`serve_requests` is strictly sequential** — it `await`s `handle_connection` before calling `accept` again: [1](#0-0) 

**`handle_connection` has no read timeout** — `stream.read` blocks until data arrives or the connection closes: [2](#0-1) 

There is no `tokio::time::timeout` wrapper, no `set_read_timeout`, and no `tokio::spawn` to handle connections concurrently. A single attacker-controlled TCP connection that never sends data permanently stalls the loop at line 40, preventing `listener.accept()` from ever being called again.

---

### Title
Single Idle TCP Connection Permanently Blocks Sequential Dashboard Loop — (`rs/orchestrator/dashboard/src/dashboard.rs`)

### Summary
The `serve_requests` loop processes connections one-at-a-time with no read timeout. One attacker-controlled TCP connection that never sends data causes `stream.read` to block indefinitely, making the dashboard permanently unresponsive.

### Finding Description
`serve_requests` accepts a connection and immediately `await`s `handle_connection` before looping back to `accept` the next one. [1](#0-0) 

Inside `handle_connection`, the first operation is an unbounded async read: [3](#0-2) 

`tokio::net::TcpStream::read` returns only when bytes arrive or the peer closes the connection. No timeout is set anywhere in the trait or its callers. If the peer holds the connection open silently, the `await` never resolves, the loop never advances, and no further `accept` calls are made.

### Impact Explanation
The orchestrator dashboard is used by monitoring systems to verify node health before triggering automated recovery. A permanently blocked dashboard means:
- Health checks return no response.
- Automated recovery actions that depend on dashboard reachability cannot trigger.
- The node appears unhealthy or unreachable to the monitoring layer.

A single TCP connection is sufficient — the 1000-connection scenario in the question is not required; one idle connection achieves the same effect.

### Likelihood Explanation
Port 7070 is reachable by any host permitted by the nftables rules. The attack requires only the ability to open a TCP connection and hold it open without sending data — trivially achievable with `nc`, `telnet`, or a raw socket. No authentication, no protocol knowledge, and no special privileges are needed.

### Recommendation
Two independent fixes should be applied:

1. **Add a per-connection read timeout** using `tokio::time::timeout`:
   ```rust
   use tokio::time::{timeout, Duration};
   timeout(Duration::from_secs(5), stream.read(&mut buffer)).await
   ```

2. **Spawn each connection on a separate task** so one slow/idle connection cannot block others:
   ```rust
   loop {
       if let Ok((stream, _)) = listener.accept().await {
           let this = self.clone(); // requires Dashboard: Clone + Send + Sync
           tokio::spawn(async move { this.handle_connection(stream).await });
       }
   }
   ```

Both mitigations together eliminate the blocking and the starvation.

### Proof of Concept
```bash
# Open one idle connection, never send data
nc -q 0 <node-ip> 7070 &

# Immediately try a legitimate health check — it will hang forever
curl --max-time 10 http://<node-ip>:7070/
# Expected: curl: (28) Operation timed out
``` [4](#0-3)

### Citations

**File:** rs/orchestrator/dashboard/src/dashboard.rs (L28-43)
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
