The code difference is confirmed. Let me verify the exact lines and check for any other guards that might mitigate this.

The code discrepancy is confirmed. The direct client at line 89 calls `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)`, while `create_socks_proxy_client` at line 119 omits it entirely. Let me check the post-receive header validation to see if it compensates.

The full picture is now clear. Here is the complete analysis:

**Key facts confirmed:**

1. `CanisterHttp::new` builds the direct client with `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)` (52 KB). [1](#0-0) 

2. `create_socks_proxy_client` builds the SOCKS client with a bare `Client::builder(TokioExecutor::new()).build(...)` — **no** `.http2_max_header_list_size()` call. [2](#0-1) 

3. The SOCKS fallback path is triggered automatically when the direct connection fails (e.g., IPv4-only targets, which is the normal case for IC nodes that are IPv6-only). [3](#0-2) 

4. The post-receive header check at lines 374–417 only compares `headers_size_bytes` against the canister-supplied `max_response_size_bytes`. It does **not** enforce `MAX_HEADER_LIST_SIZE`. Critically, this check runs **after** the h2 layer has already decoded and allocated memory for all headers. [4](#0-3) 

5. `validate_headers` only validates **outgoing request headers**, not incoming response headers. [5](#0-4) 

6. `MAX_HEADER_LIST_SIZE` is defined as 52 KB, explicitly to protect against hyper's panic on excessive header allocation. [6](#0-5) 

---

### Title
Missing `http2_max_header_list_size` on SOCKS Proxy Client Allows Unbounded Header Allocation in HTTPS Outcalls Adapter — (`rs/https_outcalls/adapter/src/rpc_server.rs`)

### Summary
`create_socks_proxy_client` omits the `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)` call that is present on the direct client in `CanisterHttp::new`. When an HTTP/2 connection is made through the SOCKS proxy, the underlying `h2` crate uses its default limit of `u32::MAX` (effectively unlimited). A canister developer who controls a malicious HTTP/2 server can cause the adapter process to allocate unbounded memory while decoding response headers, before any application-level check can intervene.

### Finding Description
In `CanisterHttp::new`, the direct client is correctly configured:
```rust
let client = Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)  // 52 KB
    .build::<_, Full<Bytes>>(direct_https_connector);
```
In `create_socks_proxy_client`, the equivalent call is absent:
```rust
Client::builder(TokioExecutor::new()).build::<_, Full<Bytes>>(
    builder.enable_all_versions().wrap_connector(SocksConnector { ... }),
)
```
The `http2_max_header_list_size` setting controls the `SETTINGS_MAX_HEADER_LIST_SIZE` value advertised to the server and, more importantly, the client-side enforcement in the `h2` crate's HPACK decoder. Without it, the `h2` crate (v0.4.13 per `Cargo.lock`) uses `u32::MAX` as the limit, meaning it will decode and heap-allocate headers of arbitrary total size before returning the response to the application layer.

The SOCKS path is reached automatically as a fallback (lines 336–365) whenever the direct connection fails — the normal case for IPv4-only targets, since IC replica nodes are IPv6-only. The canister developer controls the target URL and can point it at a malicious server they operate.

The post-receive check (lines 374–417) compares `headers_size_bytes` against `max_response_size_bytes`, but this runs **after** the h2 layer has already allocated memory for the full decoded header list. It provides no protection against the allocation itself.

### Impact Explanation
A canister developer can cause the HTTPS outcalls adapter process on any replica node that routes their request through a SOCKS proxy to allocate arbitrarily large amounts of heap memory (bounded only by available RAM) while decoding HTTP/2 response headers from a malicious server. Repeated requests can exhaust adapter process memory, causing an OOM kill of the adapter. This disrupts HTTP outcall processing for all canisters on the affected node until the adapter is restarted.

### Likelihood Explanation
The SOCKS path is the normal production path for IPv4 targets (IC nodes lack IPv4 addresses). Any canister developer can deploy a canister that makes HTTP outcalls to a server they control. The server simply needs to send an HTTP/2 HEADERS frame with a large HPACK-encoded header block. No special privileges, key material, or network-level attacks are required.

### Recommendation
Add `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)` to the `Client::builder` call in `create_socks_proxy_client`, mirroring the direct client setup in `CanisterHttp::new`:

```rust
Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)
    .build::<_, Full<Bytes>>(
        builder.enable_all_versions().wrap_connector(SocksConnector { ... }),
    )
```

### Proof of Concept
Differential test:
1. Stand up a malicious HTTP/2 TLS server that, upon receiving any request, responds with a HEADERS frame whose HPACK-encoded header list totals > 52 KB (e.g., a single header with a 100 KB value).
2. Send an identical `HttpsOutcallRequest` twice to the adapter gRPC endpoint — once with a target that resolves directly (direct client path), once with a target that forces the SOCKS fallback (IPv4-only address, with SOCKS proxy configured).
3. Observe: the direct client returns an error (h2 rejects the response at the 52 KB limit); the SOCKS client either returns the response successfully or allocates the full header block before any error, confirming the missing limit.
4. Scale up the header size to several hundred MB and observe adapter process RSS growth, confirming unbounded allocation.

### Citations

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L38-50)
```rust
/// Hyper only supports a maximum of 32768 headers https://docs.rs/hyper/1.5.0/hyper/header/index.html
/// and it panics if we try to allocate more headers. And since hyper sometimes grows the map by doubling the entries
/// we choose a lower value to be safe.
const HEADERS_LIMIT: usize = 1_024;
/// Hyper also limits the size of the HeaderName to 32768. https://docs.rs/hyper/1.5.0/hyper/header/index.html.
const HEADER_NAME_VALUE_LIMIT: usize = 8_192;

/// By default most higher-level http libs like `curl` set some `User-Agent` so we do the same here to avoid getting rejected due to strict server requirements.
const USER_AGENT_ADAPTER: &str = "ic/1.0";

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

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L374-417)
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

            let remaining_limit = req
                .max_response_size_bytes
                .checked_sub(headers_size_bytes as u64)
                .ok_or_else(|| {
                    self.metrics
                        .request_errors
                        .with_label_values(&[LABEL_HEADER_RECEIVE_SIZE])
                        .inc();
                    CanisterHttpError {
                        kind: CanisterHttpErrorKind::LimitExceeded as i32,
                        message: format!(
                            "Header size exceeds specified response size limit {}",
                            req.max_response_size_bytes
                        ),
                    }
                })?;
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L483-500)
```rust
fn validate_headers(raw_headers: Vec<HttpHeader>) -> Result<HeaderMap, Status> {
    // Check we are within limit for number of headers.
    if raw_headers.len() > HEADERS_LIMIT {
        return Err(Status::new(
            tonic::Code::InvalidArgument,
            format!("Too many headers. Maximum allowed: {HEADERS_LIMIT}"),
        ));
    }
    // Check that header name and values are within limit.
    if raw_headers
        .iter()
        .any(|h| h.name.len() > HEADER_NAME_VALUE_LIMIT || h.value.len() > HEADER_NAME_VALUE_LIMIT)
    {
        return Err(Status::new(
            tonic::Code::InvalidArgument,
            format!("Header name or value exceeds size limit of {HEADER_NAME_VALUE_LIMIT}"),
        ));
    }
```
