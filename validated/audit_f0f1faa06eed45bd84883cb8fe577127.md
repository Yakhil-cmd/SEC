Audit Report

## Title
Missing `http2_max_header_list_size` Cap on SOCKS-Proxied HTTP/2 Connections Allows Memory Exhaustion in HTTPS Outcalls Adapter — (`rs/https_outcalls/adapter/src/rpc_server.rs`)

## Summary

`create_socks_proxy_client` constructs a hyper `Client` without `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)`, unlike the direct client which enforces a 52 KiB cap. A malicious HTTPS server reachable via the SOCKS fallback path can send HTTP/2 HEADERS frames of arbitrary size that hyper buffers entirely in memory before any application-level check runs, causing a memory spike or OOM crash of the adapter process. Because the SOCKS client is cached and reused, the crash-restart loop is repeatable and cheap to sustain.

## Finding Description

`MAX_HEADER_LIST_SIZE` is defined as `52 * 1024` bytes and applied to the direct client:

```rust
// rpc_server.rs L88-90
let client = Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)
    .build::<_, Full<Bytes>>(direct_https_connector);
``` [1](#0-0) 

The SOCKS proxy client omits this call entirely:

```rust
// rpc_server.rs L119-127
Client::builder(TokioExecutor::new()).build::<_, Full<Bytes>>(
    builder
        .enable_all_versions()
        .wrap_connector(SocksConnector { proxy_addr, auth: None, connector: http_connector }),
)
``` [2](#0-1) 

A repository-wide grep confirms `http2_max_header_list_size` appears exactly once — only in the direct-client path. When the builder option is omitted, hyper-util/h2 defaults to `u32::MAX` (no limit), meaning the h2 decoder will allocate the full HEADERS frame regardless of size.

The application-level header size check at L374–417 runs **after** hyper has already decoded and heap-allocated the complete HEADERS frame: [3](#0-2) [4](#0-3) 

This check cannot prevent the allocation; `http2_max_header_list_size` is the only mechanism that causes h2 to reject oversized headers at the framing layer, before allocation.

**Trigger path:**
1. Canister issues an HTTP outcall to an IPv4-only target.
2. Direct connect fails (replica has no IPv4 interface).
3. `or_else` closure fires → `do_https_outcall_socks_proxy` → `get_socks_client` → `create_socks_proxy_client` (no header cap). [5](#0-4) 
4. Malicious server responds with an HTTP/2 HEADERS frame containing hundreds of MiB of headers.
5. h2 decoder allocates the full payload → adapter process OOM / memory spike.
6. The uncapped client is cached by proxy URI and reused on every subsequent request, enabling a sustained crash-restart loop. [6](#0-5) 

## Impact Explanation

An OOM crash kills the HTTPS outcalls adapter process on the affected replica. During the restart window, all HTTPS outcalls from that replica fail. Because the SOCKS client is cached and reused, the attacker can continuously re-trigger the crash, keeping the adapter in a crash-restart loop for as long as the malicious server is reachable. This constitutes a sustained, application/platform-level DoS against the HTTPS outcalls subsystem on the targeted replica — matching the **High** impact class: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS"* ($2,000–$10,000).

## Likelihood Explanation

- The attacker only needs to operate any HTTPS server that any canister calls via an IPv4-only address, which is the common production path on IC replicas (IPv6-only interfaces).
- No privileged access, key material, or majority corruption is required.
- The attack is repeatable and essentially free: sending a large HEADERS frame costs the attacker nothing.
- The SOCKS fallback is the standard production path for IPv4 reachability, making the vulnerable code path routinely exercised.

## Recommendation

Add `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)` to the `Client::builder` call inside `create_socks_proxy_client`, mirroring the direct-client construction:

```rust
Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)   // add this line
    .build::<_, Full<Bytes>>(
        builder.enable_all_versions().wrap_connector(SocksConnector {
            proxy_addr,
            auth: None,
            connector: http_connector,
        }),
    )
```

## Proof of Concept

1. Stand up a TLS server speaking HTTP/2 that, on any GET, responds with a HEADERS frame containing ≥200 KiB of synthetic headers (e.g., 25 headers each with an 8 KiB value).
2. Configure a local SOCKS5 proxy forwarding to this server.
3. Issue a canister HTTP outcall to an IPv4-only address so the direct connect fails, with `socks_proxy_addrs` pointing at the local proxy.
4. **Expected (direct path):** hyper rejects the response at the h2 layer with `PROTOCOL_ERROR` before allocating, because `MAX_HEADER_LIST_SIZE = 52 KiB` is enforced.
5. **Actual (SOCKS path):** hyper allocates the full payload before the adapter's application-level check runs. Scale to several hundred MiB to trigger OOM.
6. Assert that the SOCKS path returns an error without buffering oversized headers and that adapter RSS does not spike — this assertion currently fails.

### Citations

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

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L131-147)
```rust
    fn get_socks_client(
        &self,
        socks_proxy_uri: Uri,
    ) -> Client<HttpsConnector<SocksConnector<HttpConnector>>, OutboundRequestBody> {
        let cache_guard = self.cache.upgradable_read();

        if let Some(client) = cache_guard.get(&socks_proxy_uri.to_string()) {
            client.clone()
        } else {
            let mut cache_guard = RwLockUpgradableReadGuard::upgrade(cache_guard);
            self.metrics.socks_cache_misses.inc();
            let client = self.create_socks_proxy_client(socks_proxy_uri.clone());
            cache_guard.insert(socks_proxy_uri.to_string(), client.clone());
            self.metrics.socks_cache_size.set(cache_guard.len() as i64);
            client
        }
    }
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

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L374-377)
```rust
            let mut headers_size_bytes = 0;
            for (k, v) in http_resp.headers() {
                headers_size_bytes += k.as_str().len() + v.len();
            }
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L402-417)
```rust
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
