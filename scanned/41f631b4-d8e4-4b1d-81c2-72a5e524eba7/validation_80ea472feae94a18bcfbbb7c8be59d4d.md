Let me read the full relevant section of the file to confirm the exact lines.

### Title
Missing `http2_max_header_list_size` cap on SOCKS-proxy hyper client allows unbounded HTTP/2 header buffering — (`rs/https_outcalls/adapter/src/rpc_server.rs`)

---

### Summary

The direct-connection hyper `Client` is built with a 52 KiB HTTP/2 header list cap, but the SOCKS-proxy `Client` built in `create_socks_proxy_client` omits that cap entirely. A malicious HTTPS server reachable only over IPv4 (forcing the SOCKS fallback) can respond with arbitrarily large HTTP/2 HEADERS frames that hyper will buffer without limit, causing a memory spike or OOM crash in the adapter process on the affected replica.

---

### Finding Description

`MAX_HEADER_LIST_SIZE` is defined as 52 KiB: [1](#0-0) 

The direct client enforces it: [2](#0-1) 

`create_socks_proxy_client` does **not**: [3](#0-2) 

A `grep` across the entire file confirms `.http2_max_header_list_size` appears exactly once — only on the direct client. The h2 crate's client-side default for `SETTINGS_MAX_HEADER_LIST_SIZE` is `u32::MAX` (no limit), so without the call the adapter advertises no bound to the server and hyper will allocate whatever the server sends.

The SOCKS path is reached whenever the direct connection fails: [4](#0-3) 

`do_https_outcall_socks_proxy` calls `get_socks_client` → `create_socks_proxy_client`, producing the uncapped client: [5](#0-4) 

---

### Impact Explanation

A malicious server that is IPv4-only (causing the direct IPv6 connection to fail fast) can send an HTTP/2 HEADERS frame — or a HEADERS + CONTINUATION chain — carrying hundreds of KiB or more of header data. Hyper's h2 layer will allocate memory for all of it before the adapter's own response-size checks run. On a replica whose adapter is handling this request, this produces an unbounded memory spike; a sufficiently large payload can exhaust the adapter process's memory and crash it, disabling HTTPS outcalls on that replica until the process restarts.

---

### Likelihood Explanation

The attacker only needs to:
1. Operate any IPv4-only HTTPS server (trivially achievable).
2. Have a canister issue an outcall to that server — either their own canister or by convincing another canister to call an attacker-controlled URL.
3. Return an HTTP/2 response with an oversized HEADERS frame.

No privileged access, no key material, and no subnet-majority corruption is required. The SOCKS fallback is a documented, production code path exercised in integration tests.

---

### Recommendation

Add `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)` to the `Client::builder` call inside `create_socks_proxy_client`, mirroring the direct client:

```rust
// rs/https_outcalls/adapter/src/rpc_server.rs  ~line 119
Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)   // <-- add this
    .build::<_, Full<Bytes>>(
        builder
            .enable_all_versions()
            .wrap_connector(SocksConnector { ... }),
    )
``` [6](#0-5) 

---

### Proof of Concept

Differential test (sketch):

1. Spin up a mock TLS server that, upon receiving any HTTP/2 request, sends a HEADERS response whose combined header bytes exceed 200 KiB (e.g., 200 headers each with a 1 KiB value).
2. Make the server listen on `127.0.0.1` only (IPv4).
3. Configure a forwarding SOCKS5 server (as already done in `server_test.rs`) pointing at the mock server.
4. Issue a canister outcall to an unreachable IPv4 address with the SOCKS proxy address set — forcing `do_https_outcall_socks_proxy`.
5. **Expected (direct path):** adapter returns an error after ~52 KiB of headers are received.
6. **Actual (SOCKS path):** adapter buffers all 200 KiB before returning; peak RSS of the adapter process is measurably higher, and with a large enough payload the process OOMs.

### Citations

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L48-50)
```rust
/// We should support at least 48 KB in headers and values according to the IC spec:
/// "the total number of bytes representing the header names and values must not exceed 48KiB".
const MAX_HEADER_LIST_SIZE: u32 = 52 * 1024;
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L88-90)
```rust
        let client = Client::builder(TokioExecutor::new())
            .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)
            .build::<_, Full<Bytes>>(direct_https_connector);
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L101-128)
```rust
    fn create_socks_proxy_client(
        &self,
        proxy_addr: Uri,
    ) -> Client<HttpsConnector<SocksConnector<HttpConnector>>, Full<Bytes>> {
        let mut http_connector = HttpConnector::new();
        http_connector.enforce_http(false);
        http_connector
            .set_connect_timeout(Some(Duration::from_secs(self.http_connect_timeout_secs)));

        let builder = HttpsConnectorBuilder::new()
            .with_native_roots()
            .expect("Failed to set native roots");

        #[cfg(not(feature = "http"))]
        let builder = builder.https_only();
        #[cfg(feature = "http")]
        let builder = builder.https_or_http();

        Client::builder(TokioExecutor::new()).build::<_, Full<Bytes>>(
            builder
                .enable_all_versions()
                .wrap_connector(SocksConnector {
                    proxy_addr,
                    auth: None,
                    connector: http_connector,
                }),
        )
    }
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L168-199)
```rust
    async fn do_https_outcall_socks_proxy(
        &self,
        socks_proxy_addrs: Vec<String>,
        request: http::Request<Full<Bytes>>,
    ) -> Result<http::Response<Incoming>, String> {
        let mut socks_proxy_addrs = socks_proxy_addrs.to_owned();

        socks_proxy_addrs.shuffle(&mut thread_rng());

        let mut last_error = None;

        let mut tries = 0;

        for socks_proxy_addr in &socks_proxy_addrs {
            let socks_proxy_uri: Uri = match socks_proxy_addr.parse() {
                Ok(uri) => uri,
                Err(e) => {
                    debug!(self.logger, "Failed to parse SOCKS proxy address: {}", e);
                    continue;
                }
            };

            tries += 1;
            if tries > MAX_SOCKS_PROXY_TRIES {
                break;
            }

            let socks_client = self.get_socks_client(socks_proxy_uri);

            let url_format = Self::classify_uri_host(request.uri());

            match socks_client.request(request.clone()).await {
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
