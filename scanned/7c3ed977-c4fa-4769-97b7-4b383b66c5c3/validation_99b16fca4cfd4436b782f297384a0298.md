The code discrepancy is confirmed. Let me verify the SOCKS proxy address sourcing to complete the attack path analysis.

### Title
Missing `http2_max_header_list_size` Cap on SOCKS Proxy Client Allows Oversized HTTP/2 Header Allocation in Adapter Process — (`rs/https_outcalls/adapter/src/rpc_server.rs`)

---

### Summary

`create_socks_proxy_client` builds a hyper `Client` without calling `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)`, while the direct client in `CanisterHttp::new` does. A canister developer who controls a target server can trigger the SOCKS fallback path and have that server return HTTP/2 response headers far exceeding the intended 52 KB cap, causing unbounded memory allocation in the adapter process.

---

### Finding Description

`MAX_HEADER_LIST_SIZE` is defined as 52 KB and applied to the direct client: [1](#0-0) [2](#0-1) 

The SOCKS client built in `create_socks_proxy_client` omits this call entirely: [3](#0-2) 

When `http2_max_header_list_size` is not set, hyper-util/h2 defaults to `u32::MAX` — no limit. The HTTP/2 `SETTINGS_MAX_HEADER_LIST_SIZE` frame sent to the server will not advertise any cap, so a cooperative server may legally send arbitrarily large HPACK-decoded header blocks. h2 will allocate memory for all of them before returning control to the caller.

The SOCKS fallback is triggered automatically when the direct connection fails: [4](#0-3) 

The SOCKS proxy addresses are sourced from the registry (boundary node IPs at port 1080) and are not attacker-controlled: [5](#0-4) 

The boundary node SOCKS proxy is a transparent relay — it forwards the TCP stream to whatever host the canister requested. The attacker's server is the HTTP/2 peer, not the proxy itself.

After the response is received, the adapter iterates over the already-allocated headers to count bytes and build the response: [6](#0-5) 

This post-hoc accounting does not prevent the allocation from occurring inside h2's frame parser.

---

### Impact Explanation

An attacker-controlled HTTP/2 server can send a response with a header list of arbitrary size (bounded only by h2's internal frame-level limits, which are orders of magnitude larger than 52 KB). Each such request causes the adapter process to allocate memory proportional to the header list size. With multiple concurrent canister HTTP outcalls routed through SOCKS — which is the normal production path for IPv4 destinations on IPv6-only replicas — an attacker can drive the adapter process toward OOM, degrading or crashing canister HTTP outcall service on the affected replica node. This is a process-level denial of service scoped to the adapter, not the replica consensus engine.

The claim of a hyper *panic* is not accurate: h2 returns an error when the limit is set and exceeded; without the limit it simply allocates. The memory exhaustion path is the realistic impact.

---

### Likelihood Explanation

The SOCKS fallback is the documented production path for IPv4-only destinations (comment at line 340–341). A canister developer can freely choose an IPv4-only target URL pointing to a server they control. On any IPv6-only replica node (the standard IC deployment), the direct connection fails immediately, and the SOCKS path is taken. The attacker's server is then the HTTP/2 peer and can send any response headers it chooses. No privileged access, no key material, no governance majority, and no network-level attack is required.

---

### Recommendation

Add `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)` to the `Client::builder` call inside `create_socks_proxy_client`, mirroring the direct client construction in `CanisterHttp::new`:

```rust
// rs/https_outcalls/adapter/src/rpc_server.rs, create_socks_proxy_client
Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)   // add this
    .build::<_, Full<Bytes>>(
        builder
            .enable_all_versions()
            .wrap_connector(SocksConnector { ... }),
    )
```

---

### Proof of Concept

1. Deploy a canister that issues an HTTP outcall to an IPv4-only address you control (e.g., `http://203.0.113.1/`).
2. On that server, run an HTTP/2 responder that, upon any request, sends a `200 OK` with a `HEADERS` frame whose decoded header list totals, say, 10 MB (e.g., 1 000 headers each with a 10 KB value).
3. On an IPv6-only replica node, submit the canister HTTP outcall. The direct connection to the IPv4 address fails; the adapter falls back to `do_https_outcall_socks_proxy`.
4. The SOCKS client (built without `http2_max_header_list_size`) forwards the request through the boundary node SOCKS proxy to your server.
5. Your server sends the oversized `HEADERS` frame. h2 allocates ~10 MB for that single response.
6. Repeat with many concurrent outcalls. Observe adapter process RSS growing without the 52 KB-per-response bound that the direct client enforces.
7. **Differential confirmation**: send the same oversized response to the direct client (by making it reachable over IPv6). The direct client returns an `h2` protocol error and allocates nothing beyond the frame header. The SOCKS client accepts and allocates the full header list.

### Citations

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L50-50)
```rust
const MAX_HEADER_LIST_SIZE: u32 = 52 * 1024;
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L88-90)
```rust
        let client = Client::builder(TokioExecutor::new())
            .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)
            .build::<_, Full<Bytes>>(direct_https_connector);
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L119-127)
```rust
        Client::builder(TokioExecutor::new()).build::<_, Full<Bytes>>(
            builder
                .enable_all_versions()
                .wrap_connector(SocksConnector {
                    proxy_addr,
                    auth: None,
                    connector: http_connector,
                }),
        )
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L336-365)
```rust
            let http_resp = self
                .client
                .request(http_req)
                .or_else(|direct_err| async move {
                    // If we fail, we try with the socks proxy. For destinations that are ipv4 only this should
                    // fail fast because our interface does not have an ipv4 assigned.
                    self.metrics.requests_socks.inc();
                    info!(
                        self.logger,
                        "Direct connection failed, trying via socks proxies with addsrs: {:?}",
                        req.socks_proxy_addrs
                    );
                    self.do_https_outcall_socks_proxy(req.socks_proxy_addrs, http_req_clone)
                        .await
                        .map_err(|socks_err| {
                            self.metrics
                                .request_errors
                                .with_label_values(&[LABEL_CONNECT])
                                .inc();
                            CanisterHttpError {
                                kind: CanisterHttpErrorKind::Connection as i32,
                                message: format!(
                                    "Connecting to {:.50} failed: direct connect {direct_err:?}
                                    and connect through socks {socks_err:?}",
                                    uri.host().unwrap_or(""),
                                ),
                            }
                        })
                })
                .await?;
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L374-400)
```rust
            let mut headers_size_bytes = 0;
            for (k, v) in http_resp.headers() {
                headers_size_bytes += k.as_str().len() + v.len();
            }

            total_downloaded_bytes += headers_size_bytes as u64;

            let headers = http_resp
                .headers()
                .iter()
                .map(|(k, v)| {
                    let name = k.to_string();
                    let value = v.to_str()?.to_string();
                    Ok(HttpHeader { name, value })
                })
                .collect::<Result<Vec<_>, ToStrError>>()
                .map_err(|err| {
                    debug!(self.logger, "Failed to parse headers: {}", err);
                    self.metrics
                        .request_errors
                        .with_label_values(&[LABEL_RESPONSE_HEADERS])
                        .inc();
                    CanisterHttpError {
                        kind: CanisterHttpErrorKind::Internal as i32,
                        message: format!("Failed to parse headers: {err}"),
                    }
                })?;
```

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L204-249)
```rust
    fn get_socks_proxy_addrs(&self) -> Vec<String> {
        let latest_registry_version = self.registry_client.get_latest_version();

        let allowed_boundary_nodes = match self.subnet_type {
            SubnetType::System => self
                .registry_client
                .get_system_api_boundary_node_ids(latest_registry_version),
            SubnetType::Application | SubnetType::VerifiedApplication => self
                .registry_client
                .get_app_api_boundary_node_ids(latest_registry_version),
            // Cloud engines are not allowed to use the SOCKS proxy of the API BNs
            SubnetType::CloudEngine => Ok(Vec::new()),
        };

        allowed_boundary_nodes
            .unwrap_or_else(|e| {
                warn!(self.log, "Failed to get API boundary node IDs: {:?}", e);
                Vec::new()
            })
            .into_iter()
            .filter_map(|id| {
                self.registry_client
                    .get_node_record(id, latest_registry_version)
                    .map_err(|e| {
                        warn!(
                            self.log,
                            "Failed to get node record for node ID {:?}: {:?}", id, e
                        );
                    })
                    .ok()
                    .and_then(|opt_record| {
                        opt_record.or_else(|| {
                            warn!(self.log, "No node record found for node ID {:?}", id);
                            None
                        })
                    })
                    .and_then(|record| {
                        record.http.or_else(|| {
                            warn!(self.log, "HTTP information missing for node ID {:?}", id);
                            None
                        })
                    })
                    .map(|http_info| format!("socks5h://[{0}]:1080", http_info.ip_addr))
            })
            .collect::<Vec<String>>()
    }
```
