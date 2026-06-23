Based on my analysis of the actual code, here is my determination:

---

### Title
Unbounded Header Count in `read_request` Enables Memory Amplification via `http::HeaderMap` — (`rs/p2p/quic_transport/src/request_handler.rs`)

### Summary

The `read_request` function bounds raw stream bytes at `MAX_MESSAGE_SIZE_BYTES` (128 MiB) but imposes no limit on the number of protobuf headers. A Byzantine peer can craft a 128 MiB protobuf message containing ~16.8 million tiny headers. When the header-building loop constructs the `http::HeaderMap`, the per-entry struct overhead amplifies memory usage by ~16x, reaching ~2 GB per stream. With `MAX_CONCURRENT_BIDI_STREAMS = 1000`, a single Byzantine peer can trigger ~2 TB of allocation, causing OOM and replica crash.

### Finding Description

In `read_request`:

```rust
// line 173-178: raw bytes bounded at 128 MiB
let request_bytes = recv_stream
    .read_to_end(MAX_MESSAGE_SIZE_BYTES)   // MAX_MESSAGE_SIZE_BYTES = 128 MiB
    .await ...?;

// line 180: protobuf decoded — total header data ≤ 128 MiB
let request_proto = pb::HttpRequest::decode(request_bytes.as_slice())?;

// lines 201-204: NO header count limit
for h in request_proto.headers {
    let pb::HttpHeader { key, value } = h;
    request_builder = request_builder.header(key, value);  // allocates per entry
}
``` [1](#0-0) [2](#0-1) 

`MAX_MESSAGE_SIZE_BYTES` is 128 MiB: [3](#0-2) 

The amplification mechanism: each call to `request_builder.header(key, value)` causes the `http` crate to:
1. Call `HeaderName::from_bytes` → `Bytes::copy_from_slice` (new heap allocation for key)
2. Call `HeaderValue::from_bytes` → `Bytes::copy_from_slice` (new heap allocation for value)
3. Insert a `Bucket<HeaderValue>` struct (~60 bytes overhead) into the `HeaderMap`'s internal `Vec<Bucket>`

For a 1-byte key and 1-byte value header, the minimum protobuf encoding is ~8 bytes. A 128 MiB message can therefore encode ~16.8 million such headers. Each header in the `HeaderMap` costs approximately:
- `Bucket` struct: ~60 bytes
- Key heap allocation (allocator minimum block): ~32 bytes
- Value heap allocation (allocator minimum block): ~32 bytes
- **Total: ~124 bytes per header**

16.8M × 124 bytes ≈ **2.08 GB per stream** from a 128 MiB input (~16x amplification).

The stream concurrency limit is 1,000 per connection: [4](#0-3) 

Each stream is spawned as an independent tokio task with no backpressure: [5](#0-4) 

1,000 concurrent streams × 2.08 GB = **~2 TB** → OOM → replica crash.

### Impact Explanation

A single Byzantine replica node (one compromised subnet member) can crash any honest replica node it is connected to. If it targets enough honest nodes, it can disrupt subnet consensus. The QUIC transport layer is explicitly designed to operate with Byzantine peers (per the README), and the connection is mutually authenticated via TLS node certificates — no majority corruption is required.

### Likelihood Explanation

The attack requires only a single compromised subnet node with a valid TLS certificate. The crafted message is valid protobuf and passes all existing checks. The attacker controls the header count and sizes freely within the 128 MiB raw-byte budget. No special timing or race conditions are needed.

### Recommendation

Add a header count guard immediately after protobuf decode, before the loop:

```rust
const MAX_HEADERS_PER_REQUEST: usize = 100; // or similar conservative limit

if request_proto.headers.len() > MAX_HEADERS_PER_REQUEST {
    return Err(P2PError::...);
}
```

Additionally, consider bounding individual header key and value lengths independently of the total message size.

### Proof of Concept

Construct a `pb::HttpRequest` protobuf with `N = floor(128 MiB / 8)` headers, each with a 1-byte key (`"a"`) and 1-byte value (`"b"`). Encode to ≤128 MiB. Open 1,000 QUIC bidirectional streams to the victim node and send this message on each. Monitor victim RSS — it will grow to ~2 TB before the OOM killer terminates the replica process.

### Citations

**File:** rs/p2p/quic_transport/src/request_handler.rs (L79-90)
```rust
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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L173-178)
```rust
    let request_bytes = recv_stream
        .read_to_end(MAX_MESSAGE_SIZE_BYTES)
        .await
        .inspect_err(|err| {
            observe_read_to_end_error(err, "read_to_end", &metrics.request_handle_errors_total)
        })?;
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L201-204)
```rust
    for h in request_proto.headers {
        let pb::HttpHeader { key, value } = h;
        request_builder = request_builder.header(key, value);
    }
```

**File:** rs/p2p/quic_transport/src/lib.rs (L74-74)
```rust
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L74-74)
```rust
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
```
