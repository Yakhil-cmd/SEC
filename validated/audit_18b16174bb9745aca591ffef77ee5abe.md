Audit Report

## Title
Missing `http2_max_header_list_size` Cap on SOCKS Proxy Client Allows Unbounded HTTP/2 Header Allocation in Adapter Process — (`rs/https_outcalls/adapter/src/rpc_server.rs`)

## Summary
`create_socks_proxy_client` builds a hyper `Client` at line 119 without calling `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)`, while the direct client constructed at lines 88–90 applies the 52 KB cap. Because the SOCKS path is the documented production path for IPv4-only destinations on IPv6-only replica nodes, any canister developer who controls an IPv4-only HTTP/2 server can cause the adapter to allocate arbitrarily large header blocks per response, driving the adapter process toward OOM and denying HTTP outcall service on the affected replica node.

## Finding Description
`MAX_HEADER_LIST_SIZE` is defined as `52 * 1024` at line 50 and applied to the direct client:

```rust
// L88-90
let client = Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)
    .build::<_, Full<Bytes>>(direct_https_connector);
```

`create_socks_proxy_client` (L101–128) omits this call entirely:

```rust
// L119-127
Client::builder(TokioExecutor::new()).build::<_, Full<Bytes>>(
    builder
        .enable_all_versions()
        .wrap_connector(SocksConnector { proxy_addr, auth: None, connector: http_connector }),
)
```

Without `http2_max_header_list_size`, hyper-util/h2 sends no `SETTINGS_MAX_HEADER_LIST_SIZE` advertisement to the peer (defaulting to `u32::MAX`). A cooperative HTTP/2 server may therefore legally send HPACK-decoded header blocks of arbitrary size; h2 allocates memory for the full decoded header list before returning control to the caller.

The SOCKS fallback is triggered automatically when the direct connection fails (L336–365). SOCKS proxy addresses are sourced from the registry (boundary node IPs at port 1080, L204–249 in `pool_manager.rs`) and are not attacker-controlled; the boundary node SOCKS proxy is a transparent TCP relay, making the attacker's server the actual HTTP/2 peer.

The post-hoc header accounting at L374–377 iterates over already-allocated headers and does not prevent the allocation from occurring inside h2's frame parser. The `max_response_size_bytes` check at L402–417 similarly occurs after the response object is fully received.

## Impact Explanation
An attacker-controlled HTTP/2 server can send a response with a header list of arbitrary size (bounded only by h2's internal frame-level limits, orders of magnitude larger than 52 KB). Each such request causes the adapter process to allocate memory proportional to the header list size. With multiple concurrent canister HTTP outcalls routed through SOCKS — the normal production path for IPv4 destinations on IPv6-only replicas — an attacker can drive the adapter process toward OOM, crashing or degrading canister HTTP outcall service on the affected replica node. This matches the allowed High impact: **Application/platform-level DoS not based on raw volumetric DDoS** ($2,000–$10,000).

## Likelihood Explanation
Any canister developer can freely choose an IPv4-only target URL pointing to a server they control. On IPv6-only replica nodes (the standard IC deployment), the direct connection to an IPv4 address fails immediately, and the SOCKS path is taken unconditionally. No privileged access, key material, governance majority, or network-level attack is required. The attack is repeatable and scalable via concurrent outcalls.

## Recommendation
Add `.http2_max_header_list_size(MAX_HEADER_LIST_SIZE)` to the `Client::builder` call inside `create_socks_proxy_client`, mirroring the direct client construction:

```rust
// rs/https_outcalls/adapter/src/rpc_server.rs, L119
Client::builder(TokioExecutor::new())
    .http2_max_header_list_size(MAX_HEADER_LIST_SIZE)  // add this line
    .build::<_, Full<Bytes>>(
        builder
            .enable_all_versions()
            .wrap_connector(SocksConnector { proxy_addr, auth: None, connector: http_connector }),
    )
```

## Proof of Concept
1. Deploy a canister that issues an HTTP outcall to an IPv4-only address you control (e.g., `https://203.0.113.1/`).
2. On that server, run an HTTP/2 responder that returns `200 OK` with a `HEADERS` frame whose decoded header list totals ~10 MB (e.g., 1,000 headers each with a 10 KB value).
3. On an IPv6-only replica node, submit the canister HTTP outcall. The direct connection to the IPv4 address fails; the adapter falls back to `do_https_outcall_socks_proxy`.
4. The SOCKS client (built without `http2_max_header_list_size`) forwards the request through the boundary node SOCKS proxy to your server.
5. Your server sends the oversized `HEADERS` frame. h2 allocates ~10 MB for that single response.
6. Repeat with many concurrent outcalls. Observe adapter process RSS growing without the 52 KB-per-response bound that the direct client enforces.
7. **Differential confirmation**: send the same oversized response to the direct client (via an IPv6-reachable endpoint). The direct client returns an h2 protocol error and allocates nothing beyond the frame header. The SOCKS client accepts and allocates the full header list.