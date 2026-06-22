The code is confirmed. Let me analyze the exact discrepancy.

**Direct client** (`new()`, line 88–90): [1](#0-0) 

```rust
let client = Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)   // ← cap applied
    .build::<_, Full<Bytes>>(direct_https_connector);
```

**SOCKS client** (`create_socks_proxy_client()`, lines 119–127): [2](#0-1) 

```rust
Client::builder(TokioExecutor::new()).build::<_, Full<Bytes>>(  // ← NO cap
    builder.enable_all_versions().wrap_connector(SocksConnector { … }),
)
```

`MAX_HEADER_LIST_SIZE` is 52 KiB: [3](#0-2) 

The SOCKS fallback is triggered when the direct connection fails: [4](#0-3) 

---

### Title
Missing `http2_max_header_list_size` cap on SOCKS-proxied HTTP/2 client allows malicious server to exhaust adapter memory — (`rs/https_outcalls/adapter/src/rpc_server.rs`)

### Summary
`create_socks_proxy_client` builds a `hyper` HTTP/2 client without calling `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)`, unlike the direct client. A malicious HTTPS server reachable via the SOCKS path can send arbitrarily large HTTP/2 HEADERS frames, causing the `h2` layer to buffer them without bound and potentially exhausting the adapter process's memory.

### Finding Description
`CanisterHttp::new` sets `.http2_max_header_list_size(52 * 1024)` on the direct client. `create_socks_proxy_client` omits this call entirely, leaving the `h2` crate's default in place (effectively `u32::MAX`). The `h2` library enforces `SETTINGS_MAX_HEADER_LIST_SIZE` on received HEADERS frames: with the cap set, oversized headers produce a `PROTOCOL_ERROR` before allocation completes; without it, `h2` allocates the full header block. The SOCKS client is used whenever the direct connection fails — the normal case for IPv4-only destinations, which is the primary motivation for the SOCKS path.

### Impact Explanation
A malicious server can send a HEADERS frame carrying hundreds of kilobytes (or more) of headers. The adapter process allocates all of them before any application-level check runs. Repeated requests can drive the adapter to OOM. An adapter crash on one replica causes that replica to fail all HTTPS outcall attempts until the process restarts. Because HTTPS outcalls require a threshold of replicas to agree, targeting enough replicas' adapters could degrade or stall the feature subnet-wide.

### Likelihood Explanation
The attacker only needs to operate an HTTPS server with an IPv4 address (no IPv6). Any canister that issues an outcall to such a server will trigger the SOCKS path. The attacker controls the response entirely, including HTTP/2 HEADERS frames. No privileged access, key material, or network-level attack is required.

### Recommendation
Add `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)` to the `Client::builder` call inside `create_socks_proxy_client`, mirroring the direct client:

```rust
Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)   // add this
    .build::<_, Full<Bytes>>(
        builder.enable_all_versions().wrap_connector(SocksConnector { … }),
    )
```

### Proof of Concept
1. Stand up an HTTPS server (self-signed cert accepted by the adapter) on an IPv4-only address.
2. Configure it to respond with an HTTP/2 HEADERS frame containing ~200 KiB of header data.
3. Have a canister issue an outcall to that server.
4. The direct connection fails (no IPv4 on the replica interface); `do_https_outcall_socks_proxy` is called.
5. `get_socks_client` → `create_socks_proxy_client` returns a client with no header size cap.
6. `h2` buffers the full 200 KiB header block; the adapter does not return an error.
7. Repeat to drive memory usage up; compare with the direct path, which returns a `PROTOCOL_ERROR` at 52 KiB.

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
