Audit Report

## Title
Unbounded heap allocation via oversized `StateSyncId.hash` in `state_sync_advert_handler` — (`rs/p2p/state_sync_manager/src/routes/advert.rs`)

## Summary

`state_sync_advert_handler` decodes an attacker-controlled `pb::Advert` payload and constructs an `Advert` struct whose `id.hash` field is an unbounded `Vec<u8>`, with no size guard at any point in the decode path. The QUIC transport permits messages up to 128 MiB per stream. With a channel capacity of 20, a single Byzantine subnet peer can queue up to 20 × ~128 MiB ≈ 2.56 GiB of decoded `Advert` structs on the victim replica's heap, causing OOM and crashing the replica.

## Finding Description

**No hash size validation in the handler.**
`state_sync_advert_handler` calls `pb::Advert::decode(payload)` then `Advert::try_from(advert)` with no check on `id.hash` length at any point: [1](#0-0) 

**`TryFrom<pb::Advert>` performs no size check.**
The conversion delegates to `StateSyncArtifactId::from`, which blindly wraps `id.hash` into `CryptoHash(Vec<u8>)`: [2](#0-1) [3](#0-2) 

**`StateSyncId.hash` is an unconstrained `bytes` field in the proto schema.** [4](#0-3) 

The generated Rust struct confirms `hash` is a plain `Vec<u8>` with no length constraint: [5](#0-4) 

**The QUIC transport limit is 128 MiB per message.**
`read_request` enforces this limit on the raw stream, but passes the full body to the handler without any field-level validation: [6](#0-5) [7](#0-6) 

**The advert channel has capacity 20.**
Decoded `Advert` structs (with their full `Vec<u8>` hash) sit in this bounded channel until the manager loop consumes them: [8](#0-7) 

**The QUIC request handler spawns a task per stream**, so multiple concurrent requests can be in-flight simultaneously, each holding a decoded payload in memory while awaiting channel space: [9](#0-8) 

The existing comment in the request handler even acknowledges that slow handlers cause buffering up to the stream limit: [10](#0-9) 

## Impact Explanation

A Byzantine subnet peer can cause OOM on a victim replica by filling the advert channel with ~128 MiB `Advert` structs. With channel capacity 20, peak heap from the channel alone is ~2.56 GiB, plus additional in-flight allocations from concurrent QUIC tasks awaiting channel space. This crashes the replica and removes it from the subnet's active set — a targeted availability attack. This matches the **High** bounty impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation

The attacker must be an authenticated subnet peer (a Byzantine node). The IC protocol is explicitly designed to tolerate Byzantine nodes below the fault threshold, so a single compromised node causing OOM on a peer is within the realistic threat model. No threshold corruption is required — one Byzantine node suffices. The attack is repeatable: after the victim restarts, the Byzantine peer can repeat it immediately.

## Recommendation

Add a hash size check immediately after decoding, before the `Advert` is constructed or sent to the channel. The legitimate `CryptoHashOfState` is always a fixed 32-byte SHA-256 digest. Reject any advert whose `id.hash` length differs from 32 bytes.

The most robust fix is in `TryFrom<pb::Advert> for Advert` in `rs/p2p/state_sync_manager/src/utils.rs`, or directly in `state_sync_advert_handler` in `rs/p2p/state_sync_manager/src/routes/advert.rs`:

```rust
// In state_sync_advert_handler, after decode:
if advert.id.as_ref().map_or(true, |id| id.hash.len() != 32) {
    return Err(StatusCode::BAD_REQUEST);
}
```

Alternatively, enforce the check in `StateSyncArtifactId::from(p2p_pb::StateSyncId)` in `rs/interfaces/src/p2p/state_sync.rs` by returning a `Result` and rejecting non-32-byte hashes.

## Proof of Concept

```rust
// Byzantine subnet peer sends 20 concurrent requests to /state-sync/advert:
let hash = vec![0u8; 128 * 1024 * 1024 - 64]; // near MAX_MESSAGE_SIZE_BYTES
let advert = pb::Advert {
    id: Some(pb::StateSyncId { height: 1, hash }),
};
let payload = advert.encode_to_vec();
// Wrap in pb::HttpRequest targeting STATE_SYNC_ADVERT_PATH and send 20 times
// concurrently via QUIC transport to the victim replica.
// Each decoded Advert with ~128 MiB hash sits in the mpsc channel (capacity 20).
// Total heap pressure: ~2.56 GiB → OOM on victim replica.
```

A deterministic integration test can be written using `ic_p2p_test_utils` mocks (as already used in `rs/p2p/state_sync_manager/src/lib.rs` tests) by directly sending 20 oversized `(Advert, NodeId)` tuples into the `advert_sender` channel and observing that the receiver's process RSS exceeds a threshold, or by calling `state_sync_advert_handler` directly with a crafted oversized payload and measuring allocator behavior.

### Citations

**File:** rs/p2p/state_sync_manager/src/routes/advert.rs (L23-25)
```rust
    let advert: Advert = pb::Advert::decode(payload)
        .map(|advert| Advert::try_from(advert).map_err(|_| StatusCode::BAD_REQUEST))
        .map_err(|_| StatusCode::BAD_REQUEST)??;
```

**File:** rs/p2p/state_sync_manager/src/utils.rs (L20-27)
```rust
    fn try_from(advert: pb::Advert) -> Result<Self, Self::Error> {
        Ok(Advert {
            id: advert
                .id
                .map(StateSyncArtifactId::from)
                .ok_or(ProxyDecodeError::MissingField("id"))?,
        })
    }
```

**File:** rs/interfaces/src/p2p/state_sync.rs (L23-29)
```rust
impl From<p2p_pb::StateSyncId> for StateSyncArtifactId {
    fn from(id: p2p_pb::StateSyncId) -> Self {
        Self {
            height: Height::from(id.height),
            hash: CryptoHash(id.hash),
        }
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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L52-56)
```rust
    // The extreme result of a slow handler is that the stream limit will be reach, hence
    // having buffered up to the stream limit number of messages/requests.
    // A better approach will be to use a router implemented as a tower service and accept
    // streams iff the router is ready. Then the actual number of buffered messages is determined
    // by the handlers instead by the underlying implementation.
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
