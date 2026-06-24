### Title
Canister HTTP Outcalls: Missing IPv4 Private Address Range Blocking Enables SSRF from Any Canister - (`rs/https_outcalls/adapter/src/rpc_server.rs`, `rs/ic_os/config/tool/templates/ic.json5.template`)

---

### Summary

Any canister developer can submit an `http_request` management canister call with a URL targeting IPv4 private address ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) or a DNS hostname that resolves to such an address. Neither the execution environment, the HTTPS outcall adapter, nor the GuestOS nftables firewall template blocks these ranges for the `ic-http-adapter` process. The firewall only blocks `127.0.0.0/8` on ports 1–19999 for IPv4. All 13 subnet nodes will attempt the connection for a replicated request, amplifying the probe.

---

### Finding Description

The canister HTTP outcall pipeline has three layers where URL/IP validation could occur:

**Layer 1 – Execution environment** (`rs/types/types/src/canister_http.rs`):
`CanisterHttpRequestContext::generate_from_args` calls only `validate_url_length`, `validate_http_headers_and_body`, and `validate_transform_principal`. No scheme, hostname, or IP-range check is performed. The raw URL is stored in replicated state and forwarded to the adapter. [1](#0-0) [2](#0-1) 

**Layer 2 – HTTPS outcall adapter** (`rs/https_outcalls/adapter/src/rpc_server.rs`):
`https_outcall` parses the URI and (when compiled without the `http` feature) rejects non-HTTPS schemes. After that, it immediately builds and dispatches the HTTP request with no IP-range validation. The helper `classify_uri_host` only classifies the host type for metrics; it does not block anything. [3](#0-2) [4](#0-3) [5](#0-4) 

**Layer 3 – GuestOS nftables firewall template** (`rs/ic_os/config/tool/templates/ic.json5.template`):
The IPv4 OUTPUT chain has `policy accept`. The only `ic-http-adapter`-specific IPv4 block is:
```
meta skuid ic-http-adapter ip daddr { 127.0.0.0/8 } ct state { new } tcp dport { 1-19999 } reject
```
Private ranges `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, and `100.64.0.0/10` are **not** blocked. Ports 20000–65535 on `127.0.0.0/8` are also **not** blocked. [6](#0-5) 

The golden test files confirm this is the deployed configuration: [7](#0-6) [8](#0-7) 

**DNS-based bypass**: The adapter resolves DNS at connection time and makes no post-resolution IP check. A canister can register a public DNS name that resolves to `10.0.0.1` and the firewall will not intercept it, because the nftables rule matching happens on the resolved IP, which falls outside the only blocked range (`127.0.0.0/8`).

---

### Impact Explanation

A canister can probe internal datacenter services reachable from the node's IPv4 interface. For a fully-replicated request, all 13 (or more) subnet nodes simultaneously attempt the connection, turning a single canister call into a coordinated probe of internal infrastructure. Responses are returned to the canister via the transform function, enabling data exfiltration from unauthenticated internal HTTP endpoints. This is a server-side request forgery (SSRF) class vulnerability with the IC subnet acting as the unwitting proxy.

---

### Likelihood Explanation

Any canister developer with cycles can trigger this. No privileged role, governance vote, or subnet-majority corruption is required. The call path is `http_request` on the management canister, which is a standard, documented API. The attacker only needs to deploy a canister and attach sufficient cycles.

---

### Recommendation

1. **Adapter-level IP validation**: After DNS resolution (or for literal IP URLs), validate the resolved IP against a deny-list of private/reserved ranges before making the connection. This is the most robust fix because it is independent of firewall configuration.
2. **Firewall hardening**: Extend the nftables IPv4 OUTPUT block for `ic-http-adapter` to cover `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `100.64.0.0/10`, and extend the port range to cover all ports (1–65535) on `127.0.0.0/8`.
3. **Execution-layer pre-validation**: Add a coarse check in `generate_from_args` / `generate_from_flexible_args` to reject literal private IP addresses in the URL before the request enters replicated state.

---

### Proof of Concept

A canister calls the management canister `http_request` method with:
```
url = "https://10.0.0.1/internal-api"
method = GET
max_response_bytes = 2000000
```

**Execution layer** (`generate_from_args`): `validate_url_length` passes (URL is short). No IP check. The `CanisterHttpRequestContext` is stored in replicated state with `url = "https://10.0.0.1/internal-api"`. [9](#0-8) 

**Adapter** (`https_outcall`): URI parses successfully. Scheme is `https` — passes. No IP check. `hyper` connects to `10.0.0.1:443`. [10](#0-9) [11](#0-10) 

**Firewall**: The nftables OUTPUT chain for `ic-http-adapter` has no rule matching `ip daddr 10.0.0.0/8`; the default `policy accept` allows the packet through. [7](#0-6) 

All 13 subnet nodes make the connection. If an internal service responds, the response is returned to the canister.

### Citations

**File:** rs/types/types/src/canister_http.rs (L486-492)
```rust
fn validate_url_length(url: &str) -> Result<(), CanisterHttpRequestContextError> {
    let url_len = url.len();
    if url_len > MAX_CANISTER_HTTP_URL_SIZE {
        return Err(CanisterHttpRequestContextError::UrlTooLong(url_len));
    }
    Ok(())
}
```

**File:** rs/types/types/src/canister_http.rs (L510-519)
```rust
    pub fn generate_from_args(
        time: Time,
        request: &Request,
        args: CanisterHttpRequestArgs,
        node_ids: &BTreeSet<NodeId>,
        rng: &mut dyn RngCore,
    ) -> Result<Self, CanisterHttpRequestContextError> {
        validate_transform_principal(&args.transform, request.sender.get())?;
        validate_url_length(&args.url)?;
        validate_http_headers_and_body(args.headers.get(), args.body.as_ref().unwrap_or(&vec![]))?;
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L149-166)
```rust
    fn classify_uri_host(uri: &Uri) -> &str {
        let Some(host) = uri.host() else {
            return "empty";
        };

        if host.parse::<Ipv4Addr>().is_ok() {
            return "v4";
        }

        if host.starts_with('[') && host.ends_with(']') {
            let inside = &host[1..host.len() - 1];
            if inside.parse::<Ipv6Addr>().is_ok() {
                return "v6";
            }
        }

        "domain_name"
    }
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L255-282)
```rust
            let uri = req.url.parse::<Uri>().map_err(|err| {
                debug!(self.logger, "Failed to parse URL: {}", err);
                self.metrics
                    .request_errors
                    .with_label_values(&[LABEL_URL_PARSE])
                    .inc();
                CanisterHttpError {
                    kind: CanisterHttpErrorKind::InvalidInput as i32,
                    message: format!("Failed to parse URL: {err}"),
                }
            })?;

            #[cfg(not(feature = "http"))]
            if uri.scheme() != Some(&http::uri::Scheme::HTTPS) {
                use crate::metrics::LABEL_HTTP_SCHEME;
                debug!(
                    self.logger,
                    "Got request with no or http scheme specified. {}", uri
                );
                self.metrics
                    .request_errors
                    .with_label_values(&[LABEL_HTTP_SCHEME])
                    .inc();
                return Err(CanisterHttpError {
                    kind: CanisterHttpErrorKind::InvalidInput as i32,
                    message: "Url need to specify https scheme".to_string(),
                });
            }
```

**File:** rs/https_outcalls/adapter/src/rpc_server.rs (L330-365)
```rust
            let mut http_req = hyper::Request::new(Full::new(Bytes::from(req.body)));
            *http_req.headers_mut() = headers;
            *http_req.method_mut() = method;
            *http_req.uri_mut() = uri.clone();
            let http_req_clone = http_req.clone();

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

**File:** rs/ic_os/config/tool/templates/ic.json5.template (L233-237)
```text
  chain OUTPUT {\n\
    type filter hook output priority 0; policy accept;\n\
    meta skuid ic-http-adapter ip daddr { 127.0.0.0/8 } ct state { new } tcp dport { 1-19999 } reject # Block restricted localhost ic-http-adapter HTTPS access\n\
    <<IPv4_OUTBOUND_RULES>>\n\
  }\n\
```

**File:** rs/orchestrator/testdata/nftables_assigned_replica.conf.golden (L52-56)
```text
  chain OUTPUT {
    type filter hook output priority 0; policy accept;
    meta skuid ic-http-adapter ip daddr { 127.0.0.0/8 } ct state { new } tcp dport { 1-19999 } reject # Block restricted localhost ic-http-adapter HTTPS access
    meta skuid ic-http-adapter ip daddr {1.1.1.1,3.0.0.3,3.0.0.4,3.0.0.5,3.0.0.6,3.0.0.7,4.0.0.4,4.0.0.5,4.0.0.6,4.0.0.7} ct state { new } tcp dport {22,2497,4100,7070,8080,9090,9091,9100,19100,19523,19531} reject # Automatic blacklisting for ic-http-adapter
  }
```

**File:** rs/orchestrator/testdata/nftables_unassigned_replica.conf.golden (L51-55)
```text
  chain OUTPUT {
    type filter hook output priority 0; policy accept;
    meta skuid ic-http-adapter ip daddr { 127.0.0.0/8 } ct state { new } tcp dport { 1-19999 } reject # Block restricted localhost ic-http-adapter HTTPS access
    meta skuid ic-http-adapter ip daddr {1.1.1.1,3.0.0.3,3.0.0.4,3.0.0.5,3.0.0.6,3.0.0.7,4.0.0.4,4.0.0.5,4.0.0.6,4.0.0.7} ct state { new } tcp dport {22,2497,4100,7070,8080,9090,9091,9100,19100,19523,19531} reject # Automatic blacklisting for ic-http-adapter
  }
```
