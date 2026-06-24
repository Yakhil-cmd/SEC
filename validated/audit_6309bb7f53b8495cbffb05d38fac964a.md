Audit Report

## Title
Unbounded Header Count Causes Multi-Layer Memory Amplification Per Stream, Enabling OOM Crash via Concurrent Streams — (`rs/p2p/quic_transport/src/request_handler.rs`)

## Summary
`read_request` in `rs/p2p/quic_transport/src/request_handler.rs` processes incoming QUIC streams with no limit on the number of headers in a `pb::HttpRequest`. A single protobuf message within the 128 MB wire limit can carry tens of thousands of headers, causing three sequential heap allocations (raw bytes, decoded `Vec<HttpHeader>`, and `http::HeaderMap`) that together amplify memory usage 2–3× over wire size. With `MAX_CONCURRENT_BIDI_STREAMS = 1_000` and no backpressure in `start_stream_acceptor`, a single Byzantine subnet peer can drive the victim replica into OOM by opening 1,000 concurrent streams each carrying a header-heavy message.

## Finding Description
The exploit path in `read_request` (`rs/p2p/quic_transport/src/request_handler.rs`, lines 169–208) proceeds through three allocation layers with no header-count guard at any point:

**Layer 1 — raw wire buffer** (lines 173–178): `recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES)` accumulates the full stream into a `Vec<u8>` of up to 128 MB. This allocation persists for the entire lifetime of `read_request`.

**Layer 2 — protobuf decode** (line 180): `pb::HttpRequest::decode(request_bytes.as_slice())` allocates a `pb::HttpRequest` on the heap, including a `Vec<HttpHeader>` whose aggregate size mirrors the header portion of the wire payload. Both `request_bytes` and `request_proto` are live simultaneously.

**Layer 3 — HeaderMap construction** (lines 201–204): The unbounded `for h in request_proto.headers` loop moves each header into `http::Request`'s internal `http::HeaderMap` (a hash table). During iteration the `Vec<HttpHeader>` backing allocation remains live while the `HeaderMap` grows, so both are resident simultaneously.

No check on `request_proto.headers.len()` exists anywhere in this path. The proto schema (`rs/protobuf/def/transport/v1/quic.proto`, lines 23–28) places no constraint on the `repeated HttpHeader headers` field. The stream acceptor (`start_stream_acceptor`, lines 43–110) spawns a new tokio task for every accepted stream into an unbounded `JoinSet` with no semaphore or backpressure, as the code comment at lines 52–56 explicitly acknowledges. `MAX_CONCURRENT_BIDI_STREAMS = 1_000` (`rs/p2p/quic_transport/src/connection_manager.rs`, line 74) allows the peer to hold 1,000 streams open simultaneously. `MAX_MESSAGE_SIZE_BYTES = 128 MB` (`rs/p2p/quic_transport/src/lib.rs`, line 74) is the only guard, and it operates only on the raw wire size, not on the amplified in-process allocation.

`STREAM_RECEIVE_WINDOW = 4 MB` (connection_manager.rs, line 73) paces QUIC-layer delivery per stream but does not cap the application-level `Vec<u8>` that `read_to_end` accumulates; the window advances continuously as the application reads, so the full 128 MB can be buffered per stream. The connection-level `RECEIVE_WINDOW = 200 MB` (line 71) similarly limits in-flight QUIC bytes, not the application-layer allocations that outlive each read.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

Concrete per-stream budget with 50,000 headers × 1 KB key + 1 KB value (~100 MB wire size, within the 128 MB cap):

| Layer | Allocation |
|---|---|
| `read_to_end` raw buffer | ~100 MB |
| `Vec<HttpHeader>` from decode | ~100 MB |
| `http::HeaderMap` hash table | ~50–100 MB |
| **Per-stream total** | **~250–300 MB** |

Across 1,000 concurrent streams: ~250–300 GB heap pressure → OOM kill of the replica process. The attack is repeatable: after the replica restarts, the Byzantine peer can immediately re-establish its connection (its TLS certificate remains valid in the registry) and repeat the attack, causing sustained unavailability of the targeted replica.

## Likelihood Explanation
The attacker must be a Byzantine subnet member — its TLS certificate is registered and a QUIC connection is already established, which is explicitly within the IC's Byzantine fault model (up to f nodes). No threshold corruption is required; a single Byzantine peer suffices. The attack requires only constructing a valid protobuf message with many small headers — trivially achievable with any protobuf library. No race condition, special timing, or external dependency is needed. The attack is deterministic and repeatable across replica restarts.

## Recommendation
1. **Add a header count limit** immediately after decode in `read_request` (`rs/p2p/quic_transport/src/request_handler.rs`, after line 180):
   ```rust
   const MAX_HEADERS_PER_REQUEST: usize = 100;
   if request_proto.headers.len() > MAX_HEADERS_PER_REQUEST {
       return Err(P2PError::from("too many headers".to_string()));
   }
   ```
2. **Add per-header key and value size limits** to bound the size of each individual header string/bytes allocation.
3. **Add a concurrent-stream semaphore** in `start_stream_acceptor` to bound total in-flight tasks from a single peer, rather than accepting up to `MAX_CONCURRENT_BIDI_STREAMS` unconditionally into an unbounded `JoinSet`.

## Proof of Concept
```rust
// Byzantine peer: open 1,000 concurrent streams, each with 50,000 headers × 2 KB
for _ in 0..1000 {
    let (mut send, _recv) = conn.open_bi().await.unwrap();
    let headers: Vec<pb::HttpHeader> = (0..50_000).map(|_| pb::HttpHeader {
        key: "x".repeat(1024),
        value: vec![0u8; 1024],
    }).collect();
    let req = pb::HttpRequest {
        uri: "/".to_string(),
        headers,
        method: pb::HttpMethod::Get as i32,
        body: vec![],
    };
    let bytes = req.encode_to_vec(); // ~100 MB, within 128 MB limit
    send.write_all(&bytes).await.unwrap();
    send.finish().unwrap();
    // Do NOT await response — keep all streams open concurrently
}
// Victim replica: 1,000 tasks each allocate ~250 MB → ~250 GB → OOM kill
```

A deterministic integration test can be written using `PocketIC` or a local two-node setup: spawn a mock Byzantine peer that opens `MAX_CONCURRENT_BIDI_STREAMS` streams each carrying a header-heavy protobuf, and assert that the victim process's RSS exceeds a threshold or that the process is killed by the OOM killer. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/p2p/quic_transport/src/request_handler.rs (L52-56)
```rust
    // The extreme result of a slow handler is that the stream limit will be reach, hence
    // having buffered up to the stream limit number of messages/requests.
    // A better approach will be to use a router implemented as a tower service and accept
    // streams iff the router is ready. Then the actual number of buffered messages is determined
    // by the handlers instead by the underlying implementation.
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L72-90)
```rust
            bi = conn_handle.conn().accept_bi() => {
                match bi {
                    Ok((bi_tx, bi_rx)) => {
                        let send_stream = ResetStreamOnDrop::new(
                            bi_tx,
                            metrics.connection_handle_incoming_streams_total.clone()
                        );
                        inflight_requests.spawn(
                            metrics.request_task_monitor.instrument(
                                handle_bi_stream(
                                    peer_id,
                                    conn_handle.conn_id(),
                                    metrics.clone(),
                                    router.clone(),
                                    send_stream,
                                    bi_rx
                                )
                            )
                        );
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L169-208)
```rust
async fn read_request(
    mut recv_stream: RecvStream,
    metrics: &QuicTransportMetrics,
) -> Result<Request<Body>, P2PError> {
    let request_bytes = recv_stream
        .read_to_end(MAX_MESSAGE_SIZE_BYTES)
        .await
        .inspect_err(|err| {
            observe_read_to_end_error(err, "read_to_end", &metrics.request_handle_errors_total)
        })?;

    let request_proto = pb::HttpRequest::decode(request_bytes.as_slice())?;
    let pb_http_method = pb::HttpMethod::try_from(request_proto.method)?;
    let http_method = match pb_http_method {
        pb::HttpMethod::Get => Some(Method::GET),
        pb::HttpMethod::Post => Some(Method::POST),
        pb::HttpMethod::Put => Some(Method::PUT),
        pb::HttpMethod::Delete => Some(Method::DELETE),
        pb::HttpMethod::Head => Some(Method::HEAD),
        pb::HttpMethod::Options => Some(Method::OPTIONS),
        pb::HttpMethod::Connect => Some(Method::CONNECT),
        pb::HttpMethod::Patch => Some(Method::PATCH),
        pb::HttpMethod::Trace => Some(Method::TRACE),
        pb::HttpMethod::Unspecified => None,
    };
    let mut request_builder = Request::builder();
    if let Some(http_method) = http_method {
        request_builder = request_builder.method(http_method);
    }
    request_builder = request_builder
        .version(Version::HTTP_3)
        .uri(request_proto.uri);
    for h in request_proto.headers {
        let pb::HttpHeader { key, value } = h;
        request_builder = request_builder.header(key, value);
    }
    // This consumes the body without requiring allocation or cloning the whole content.
    let body_bytes = Bytes::from(request_proto.body);
    Ok(request_builder.body(Body::from(body_bytes))?)
}
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L71-75)
```rust
const RECEIVE_WINDOW: VarInt = VarInt::from_u32(200_000_000);
const SEND_WINDOW: u64 = 100_000_000;
const STREAM_RECEIVE_WINDOW: VarInt = VarInt::from_u32(4_000_000);
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);
```

**File:** rs/p2p/quic_transport/src/lib.rs (L74-74)
```rust
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```

**File:** rs/protobuf/def/transport/v1/quic.proto (L23-28)
```text
message HttpRequest {
  string uri = 1;
  repeated HttpHeader headers = 2;
  HttpMethod method = 3;
  bytes body = 4;
}
```
