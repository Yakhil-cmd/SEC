Looking at the full call chain from the P2P transport layer through to `TryFrom<pb::BasicSignature>`, here is my analysis:

---

### Title
Oversized `BasicSig` Allocation Before Crypto Check via Byzantine P2P Peer — (`rs/types/types/src/signature.rs`, `rs/p2p/quic_transport/src/lib.rs`, `rs/p2p/consensus_manager/src/receiver.rs`)

### Summary

A Byzantine subnet peer can craft a `SlotUpdate` protobuf containing a `ConsensusMessage` whose embedded `BasicSignature.signature` field is padded to near the transport maximum. The receiving replica allocates the full `Vec<u8>` during `TryFrom<pb::BasicSignature>` with no size check, before any cryptographic verification. With up to 1,000 concurrent QUIC streams per peer, this can cause hundreds of gigabytes of memory allocation on a single replica.

### Finding Description

**Step 1 — No size check in `TryFrom`.** [1](#0-0) 

`BasicSig(value.signature)` wraps the raw `Vec<u8>` from the protobuf field unconditionally. The proto definition has no `max_length` constraint: [2](#0-1) 

**Step 2 — Transport cap is 128 MB, not a tight bound.**

The QUIC transport enforces a single per-message cap: [3](#0-2) 

`read_to_end(MAX_MESSAGE_SIZE_BYTES)` in the request handler reads up to 128 MB per stream before any application-level processing: [4](#0-3) 

**Step 3 — Artifact is deserialized before slot-limit check.**

In the consensus manager receiver, the `update_handler` decodes the full artifact (including the oversized `BasicSignature`) from the raw bytes and calls `TryFrom` **before** sending it to the `ConsensusManagerReceiver` event loop where the slot limit is enforced: [5](#0-4) 

The axum body limit is explicitly disabled for this route: [6](#0-5) 

**Step 4 — Concurrent stream amplification.**

The QUIC connection manager allows up to 1,000 concurrent bidirectional streams per peer: [7](#0-6) 

Each stream runs its own `handle_bi_stream` task, each of which can independently allocate up to 128 MB before the channel backpressure (capacity 100) applies. With 1,000 concurrent streams, a single Byzantine peer can force up to **~128 GB** of heap allocation before any slot-limit or crypto check.

### Impact Explanation

Single-replica OOM or severe memory pressure during consensus artifact ingestion. The affected replica stalls or crashes, dropping out of the subnet's active set until restarted. This does not break consensus safety (the subnet continues with remaining honest replicas), but it degrades liveness and can be repeated to keep a target replica offline.

### Likelihood Explanation

The attacker must be a valid subnet member (Byzantine peer below the fault threshold). TLS mutual authentication on the QUIC transport ensures only registered subnet nodes can connect. This is a realistic attacker model explicitly considered by the IC's Byzantine fault model. The attack requires no special privileges beyond subnet membership and is trivially scriptable.

### Recommendation

1. Add a size check in `TryFrom<pb::BasicSignature>` (and the analogous `ThresholdSignature`, `ThresholdSignatureShare` conversions) that rejects any signature field exceeding the maximum expected cryptographic signature size (e.g., 256 bytes for Ed25519/BLS).
2. Enforce the check before the `Vec<u8>` is materialized, or use a bounded wrapper type.
3. Consider reducing `MAX_MESSAGE_SIZE_BYTES` to a value closer to the actual maximum legitimate artifact size, or enforce per-artifact-type size limits at the transport/handler boundary.

### Proof of Concept

```rust
// Craft a SlotUpdate containing a ConsensusMessage with a 100MB BasicSignature
let oversized_sig = vec![0u8; 100 * 1024 * 1024];
let pb_basic_sig = pb::BasicSignature {
    signature: oversized_sig,
    signer: Some(valid_node_id_proto()),
};
// Wrap in a RandomBeaconShare (or any ConsensusMessage variant using BasicSignature)
// Encode as SlotUpdate and send over QUIC to the target replica.
// The replica will allocate 100MB in BasicSig(value.signature) before any crypto call.
// Repeat across 1000 concurrent QUIC streams for ~100GB total allocation.
let result = BasicSignature::<RandomBeaconContent>::try_from(pb_basic_sig);
assert!(result.is_ok()); // No rejection — allocation already occurred
```

### Citations

**File:** rs/types/types/src/signature.rs (L86-94)
```rust
impl<T> TryFrom<pb::BasicSignature> for BasicSignature<T> {
    type Error = ProxyDecodeError;
    fn try_from(value: pb::BasicSignature) -> Result<Self, Self::Error> {
        Ok(Self {
            signature: BasicSigOf::new(BasicSig(value.signature)),
            signer: node_id_try_from_option(value.signer)?,
        })
    }
}
```

**File:** rs/protobuf/def/types/v1/signature.proto (L7-10)
```text
message BasicSignature {
  bytes signature = 1;
  NodeId signer = 2;
}
```

**File:** rs/p2p/quic_transport/src/lib.rs (L72-74)
```rust
/// On purpose the value is big, otherwise there is risk of not processing important consensus messages.
/// E.g. summary blocks generated by the consensus protocol for 40 node subnet can be bigger than 5MB.
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L40-53)
```rust
pub fn build_axum_router<Artifact: PbArtifact>(
    log: ReplicaLogger,
) -> (Router, Receiver<(SlotUpdate<Artifact>, NodeId, ConnId)>) {
    let (update_tx, update_rx) = tokio::sync::mpsc::channel(100);
    let router = Router::new()
        .route(
            &format!("/{}/update", uri_prefix::<Artifact>()),
            any(update_handler),
        )
        .with_state((log, update_tx))
        // Disable request size limit since consensus might push artifacts larger than limit.
        .layer(DefaultBodyLimit::disable());

    (router, update_rx)
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L79-121)
```rust
async fn update_handler<Artifact: PbArtifact>(
    State((log, sender)): State<(ReplicaLogger, InboundSlotUpdatesSender<Artifact>)>,
    Extension(peer): Extension<NodeId>,
    Extension(conn_id): Extension<ConnId>,
    payload: Bytes,
) -> Result<(), UpdateHandlerError<Artifact>> {
    let pb_slot_update = pb::SlotUpdate::decode(payload)
        .map_err(|e| UpdateHandlerError::SlotUpdateDecoding::<Artifact>(e))?;

    let update = SlotUpdate {
        commit_id: CommitId::from(pb_slot_update.commit_id),
        slot_number: SlotNumber::from(pb_slot_update.slot_id),
        update: match pb_slot_update.update {
            Some(pb::slot_update::Update::Id(id)) => {
                let id: Artifact::Id = Artifact::PbId::decode(id.as_slice())
                    .map_err(|e| UpdateHandlerError::IdDecoding(e))
                    .and_then(|pb_id| {
                        pb_id
                            .try_into()
                            .map_err(|e| UpdateHandlerError::IdPbConversion(e))
                    })?;
                Update::Id(id)
            }
            Some(pb::slot_update::Update::Artifact(artifact)) => {
                let message: Artifact = Artifact::PbMessage::decode(artifact.as_slice())
                    .map_err(|e| UpdateHandlerError::MessageDecoding(e))
                    .and_then(|pb_msg| {
                        pb_msg
                            .try_into()
                            .map_err(|e| UpdateHandlerError::MessagePbConversion(e))
                    })?;
                Update::Artifact(message)
            }
            None => return Err(UpdateHandlerError::MissingUpdate),
        },
    };

    if sender.send((update, peer, conn_id)).await.is_err() {
        error!(log, "Failed to send slot update from handler to event loop")
    }

    Ok(())
}
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L74-75)
```rust
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);
```
