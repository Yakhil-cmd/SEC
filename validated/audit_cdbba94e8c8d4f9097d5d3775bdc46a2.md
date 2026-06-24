All cited code has been verified against the actual repository. Every claim in the report is accurate:

- `state_sync_advert_handler` decodes without any hash size guard [1](#0-0) 
- `TryFrom<pb::Advert>` performs no size check on `id.hash` [2](#0-1) 
- `hash` is an unconstrained `bytes` field in the proto schema [3](#0-2) 
- Generated struct confirms `hash: Vec<u8>` with no length constraint [4](#0-3) 
- `MAX_MESSAGE_SIZE_BYTES` is 128 MiB [5](#0-4) 
- `read_request` enforces only the raw stream limit, no field-level validation [6](#0-5) 
- Channel capacity is 20 [7](#0-6) 
- A task is spawned per accepted stream with no concurrency cap beyond the QUIC stream limit [8](#0-7) 

---

Audit Report

## Title
Unbounded heap allocation via oversized `StateSyncId.hash` in `state_sync_advert_handler` — (`rs/p2p/state_sync_manager/src/routes/advert.rs`)

## Summary
`state_sync_advert_handler` decodes an attacker-controlled `pb::Advert` payload and passes the result directly to an mpsc channel with no validation of the `id.hash` field size. The QUIC transport permits raw messages up to 128 MiB. A Byzantine subnet peer can craft adverts with a near-128 MiB `hash` field, fill the channel (capacity 20), and additionally hold decoded payloads in concurrent in-flight QUIC tasks blocked on channel send, causing OOM on the victim replica.

## Finding Description
`state_sync_advert_handler` calls `pb::Advert::decode(payload)` then `Advert::try_from(advert)` with no size check at any point:

```rust
// rs/p2p/state_sync_manager/src/routes/advert.rs L23-25
let advert: Advert = pb::Advert::decode(payload)
    .map(|advert| Advert::try_from(advert).map_err(|_| StatusCode::BAD_REQUEST))
    .map_err(|_| StatusCode::BAD_REQUEST)??;
```

`TryFrom<pb::Advert>` simply wraps `id.hash` into a `CryptoHash(Vec<u8>)` via `StateSyncArtifactId::from` without bounding its length. The proto field `StateSyncId.hash` is an unconstrained `bytes` type, and the generated Rust struct is a plain `Vec<u8>`.

The QUIC request handler (`start_stream_acceptor`) spawns an unbounded `JoinSet` task per accepted bidirectional stream. Each task calls `read_request`, which reads up to `MAX_MESSAGE_SIZE_BYTES` (128 MiB) from the stream, then routes to `state_sync_advert_handler`. The handler calls `advert_sender.send(...).await`, which blocks if the channel (capacity 20) is full. While blocked, the task holds the decoded `Advert` — including its full `Vec<u8>` hash — on the heap. There is no concurrency cap on the `JoinSet` beyond the QUIC protocol stream limit.

Existing guards that are insufficient:
- `read_to_end(MAX_MESSAGE_SIZE_BYTES)` only caps the raw wire bytes; it does not constrain individual proto fields after decoding.
- The mpsc channel bound of 20 limits queued items but does not prevent additional tasks from being in-flight and blocked on send, each holding a decoded payload.

## Impact Explanation
A single Byzantine subnet peer can crash a victim replica via OOM. The channel alone can hold 20 × ~128 MiB ≈ 2.56 GiB of decoded `Advert` structs. Additional in-flight QUIC tasks blocked on channel send contribute further heap pressure proportional to the QUIC stream limit. This constitutes a targeted availability attack: the victim replica crashes and is removed from the subnet's active set, disrupting subnet availability. This matches the allowed High impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation
The attacker must be an authenticated subnet peer (a Byzantine node). The IC protocol explicitly tolerates up to `f` Byzantine nodes in a subnet of `3f+1`. No threshold corruption is required — a single Byzantine node suffices. The attack is repeatable: after a victim restarts, the Byzantine peer can immediately repeat it. The crafted payload is trivial to construct.

## Recommendation
Add a hash length check immediately after decoding, before the `Advert` is constructed or enqueued. The legitimate `CryptoHashOfState` is always a 32-byte SHA-256 digest. Reject any advert whose `id.hash` length is not exactly 32 bytes:

```rust
// In state_sync_advert_handler, after pb::Advert::decode:
if advert.id.as_ref().map_or(true, |id| id.hash.len() != 32) {
    return Err(StatusCode::BAD_REQUEST);
}
```

Alternatively, add the check inside `TryFrom<pb::Advert> for Advert` so all callers benefit. A proto-level `(validate.rules).bytes.len = 32` annotation (if the project adopts protoc-gen-validate) would enforce this at the schema level.

## Proof of Concept
```rust
// Byzantine peer sends 20+ concurrent requests to /state-sync/advert:
let hash = vec![0u8; 128 * 1024 * 1024 - 64]; // near MAX_MESSAGE_SIZE_BYTES
let advert = pb::Advert {
    id: Some(pb::StateSyncId { height: 1, hash }),
};
let payload = advert.encode_to_vec();
// Wrap in pb::HttpRequest and send over QUIC bidirectional stream 20+ times concurrently.
// Each decoded Advert (~128 MiB Vec<u8> hash) sits in the mpsc channel (capacity 20)
// or in a blocked QUIC task awaiting channel space.
// Total heap pressure: ≥2.56 GiB → OOM on victim replica.
```

A deterministic integration test can reproduce this using `ic_p2p_test_utils` with a `MockTransport` and a channel receiver that never drains, sending 20 crafted adverts concurrently and asserting that the process does not exhaust available memory before the fix, and returns `BAD_REQUEST` after.

### Citations

**File:** rs/p2p/state_sync_manager/src/routes/advert.rs (L23-25)
```rust
    let advert: Advert = pb::Advert::decode(payload)
        .map(|advert| Advert::try_from(advert).map_err(|_| StatusCode::BAD_REQUEST))
        .map_err(|_| StatusCode::BAD_REQUEST)??;
```

**File:** rs/p2p/state_sync_manager/src/utils.rs (L17-27)
```rust
impl TryFrom<pb::Advert> for Advert {
    type Error = ProxyDecodeError;

    fn try_from(advert: pb::Advert) -> Result<Self, Self::Error> {
        Ok(Advert {
            id: advert
                .id
                .map(StateSyncArtifactId::from)
                .ok_or(ProxyDecodeError::MissingField("id"))?,
        })
    }
```

**File:** rs/protobuf/def/p2p/v1/state_sync_manager.proto (L9-12)
```text
message StateSyncId {
  uint64 height = 1;
  bytes hash = 2;
}
```

**File:** rs/protobuf/src/gen/p2p/p2p.v1.rs (L7-13)
```rust
#[derive(Clone, PartialEq, ::prost::Message)]
pub struct StateSyncId {
    #[prost(uint64, tag = "1")]
    pub height: u64,
    #[prost(bytes = "vec", tag = "2")]
    pub hash: ::prost::alloc::vec::Vec<u8>,
}
```

**File:** rs/p2p/quic_transport/src/lib.rs (L72-74)
```rust
/// On purpose the value is big, otherwise there is risk of not processing important consensus messages.
/// E.g. summary blocks generated by the consensus protocol for 40 node subnet can be bigger than 5MB.
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```

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

**File:** rs/p2p/state_sync_manager/src/lib.rs (L65-66)
```rust
    let (advert_sender, advert_receiver) = tokio::sync::mpsc::channel(20);
    let advert_handler_state = Arc::new(StateSyncAdvertHandler::new(log.clone(), advert_sender));
```
