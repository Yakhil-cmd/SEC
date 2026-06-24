Audit Report

## Title
Unbounded `BasicSignature` Allocation Before Crypto Check via Byzantine P2P Peer — (`rs/types/types/src/signature.rs`, `rs/p2p/quic_transport/src/request_handler.rs`, `rs/p2p/consensus_manager/src/receiver.rs`)

## Summary

`TryFrom<pb::BasicSignature>` unconditionally wraps the raw protobuf `signature` bytes into a heap-allocated `Vec<u8>` with no size bound, before any cryptographic verification. The QUIC transport allows up to 1,000 concurrent bidirectional streams per peer and reads up to 128 MB per stream before any application-level check. A Byzantine subnet peer can open 1,000 streams simultaneously, each carrying a near-128 MB `BasicSignature`, forcing hundreds of gigabytes of heap allocation on the target replica before any slot-limit or crypto check is reached, causing OOM and replica crash.

## Finding Description

**Step 1 — No size check in `TryFrom<pb::BasicSignature>`.**

`BasicSig(value.signature)` wraps the raw `Vec<u8>` from the protobuf field unconditionally with no length validation: [1](#0-0) 

The same pattern applies to `ThresholdSignature` and `ThresholdSignatureShare` conversions: [2](#0-1) 

**Step 2 — Transport cap is 128 MB, not a tight bound.**

`MAX_MESSAGE_SIZE_BYTES` is intentionally set large to accommodate summary blocks: [3](#0-2) 

`read_request` calls `recv_stream.read_to_end(MAX_MESSAGE_SIZE_BYTES)`, allocating up to 128 MB of raw bytes per stream before any application-level processing: [4](#0-3) 

Note: `STREAM_RECEIVE_WINDOW` is 4 MB, but this is a flow-control window, not a message size cap — `read_to_end` reads through it incrementally until the stream closes or 128 MB is reached. [5](#0-4) 

**Step 3 — Each stream spawns an independent task that fully deserializes before any slot-limit check.**

`start_stream_acceptor` spawns a new `handle_bi_stream` task for every accepted stream without any backpressure before spawning: [6](#0-5) 

The code comment at lines 52–56 explicitly acknowledges that up to `stream_limit` messages can be buffered simultaneously. Each spawned task independently calls `read_request` (128 MB allocation) and then `update_handler`, which fully decodes the `SlotUpdate` protobuf — including calling `TryFrom<pb::BasicSignature>` — before sending to the channel: [7](#0-6) 

The axum body limit is explicitly disabled for this route: [8](#0-7) 

**Step 4 — Slot-limit check is downstream of the allocation.**

The slot limit is enforced only inside `handle_slot_update_receive` in the `ConsensusManagerReceiver` event loop, which runs after the channel receive — long after the full artifact (including the oversized `BasicSignature`) has been allocated in `update_handler`: [9](#0-8) 

**Step 5 — Concurrent stream amplification.**

1,000 concurrent bidirectional streams are permitted per peer: [10](#0-9) 

The channel capacity is 100. Once full, `sender.send(...)` in `update_handler` blocks, but the raw bytes from `read_to_end` and the decoded `BasicSignature` `Vec<u8>` are already resident in heap memory for all 1,000 concurrent tasks. Peak allocation: ~1,000 × 128 MB ≈ 128 GB.

## Impact Explanation

Single-replica OOM or severe memory pressure during consensus artifact ingestion. The affected replica stalls or crashes, dropping out of the subnet's active set until restarted. This maps to the allowed High impact: **"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."** Consensus safety is not broken (the subnet continues with remaining honest replicas), but liveness is degraded and the attack can be repeated to keep a target replica offline indefinitely.

## Likelihood Explanation

The attacker must be a valid subnet member (Byzantine peer below the fault threshold). TLS mutual authentication on the QUIC transport ensures only registered subnet nodes can connect, which is a constraint. However, this is the realistic attacker model explicitly considered by the IC's Byzantine fault model. The attack requires no special privileges beyond subnet membership, is trivially scriptable, and is repeatable — the attacker can re-establish streams after each crash cycle.

## Recommendation

1. Add a size check in `TryFrom<pb::BasicSignature>` (and the analogous `ThresholdSignature`, `ThresholdSignatureShare` conversions in `rs/types/types/src/signature.rs`) that rejects any `signature` field exceeding the maximum expected cryptographic signature size (e.g., 96 bytes for BLS12-381, 64 bytes for Ed25519).
2. Enforce the check before the `Vec<u8>` is materialized, or use a bounded wrapper type.
3. Consider enforcing per-artifact-type size limits at the transport/handler boundary (e.g., in `update_handler`) before protobuf decoding, rather than relying solely on the 128 MB transport cap.
4. Consider reducing `MAX_CONCURRENT_BIDI_STREAMS` or adding per-connection memory accounting to bound total in-flight allocation.

## Proof of Concept

```rust
// Craft a SlotUpdate containing a ConsensusMessage with a ~128MB BasicSignature.
// Send over QUIC to the target replica from a Byzantine subnet peer.
let oversized_sig = vec![0u8; 127 * 1024 * 1024]; // just under 128MB cap
let pb_basic_sig = pb::BasicSignature {
    signature: oversized_sig,
    signer: Some(valid_node_id_proto()),
};
// Wrap in any ConsensusMessage variant using BasicSignature (e.g. RandomBeaconShare).
// Encode as SlotUpdate, wrap in pb::HttpRequest, send over QUIC bidi stream.
// The replica allocates ~128MB in BasicSig(value.signature) before any crypto call.
// Repeat across 1,000 concurrent QUIC streams for ~128GB total allocation.

// Verify no rejection occurs at the TryFrom boundary:
let result = BasicSignature::<RandomBeaconContent>::try_from(pb_basic_sig);
assert!(result.is_ok()); // allocation already occurred, no size check

// Integration test plan:
// 1. Spin up a local replica with PocketIC or a single-node testnet.
// 2. Connect as a registered peer (use test TLS credentials).
// 3. Open 1,000 concurrent QUIC bidi streams.
// 4. On each stream, send a crafted SlotUpdate with a 127MB BasicSignature.
// 5. Observe replica OOM / process termination via metrics or process monitor.
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

**File:** rs/types/types/src/signature.rs (L105-133)
```rust
impl<T> TryFrom<pb::ThresholdSignature> for ThresholdSignature<T> {
    type Error = ProxyDecodeError;

    fn try_from(value: pb::ThresholdSignature) -> Result<Self, Self::Error> {
        Ok(Self {
            signature: CombinedThresholdSigOf::new(CombinedThresholdSig(value.signature)),
            signer: try_from_option_field(value.signer, "ThresholdSignature::signer")?,
        })
    }
}

impl<T> From<ThresholdSignatureShare<T>> for pb::ThresholdSignatureShare {
    fn from(value: ThresholdSignatureShare<T>) -> Self {
        Self {
            signature: value.signature.get().0,
            signer: Some(node_id_into_protobuf(value.signer)),
        }
    }
}

impl<T> TryFrom<pb::ThresholdSignatureShare> for ThresholdSignatureShare<T> {
    type Error = ProxyDecodeError;

    fn try_from(value: pb::ThresholdSignatureShare) -> Result<Self, Self::Error> {
        Ok(Self {
            signature: ThresholdSigShareOf::new(ThresholdSigShare(value.signature)),
            signer: node_id_try_from_option(value.signer)?,
        })
    }
```

**File:** rs/p2p/quic_transport/src/lib.rs (L72-74)
```rust
/// On purpose the value is big, otherwise there is risk of not processing important consensus messages.
/// E.g. summary blocks generated by the consensus protocol for 40 node subnet can be bigger than 5MB.
pub(crate) const MAX_MESSAGE_SIZE_BYTES: usize = 128 * 1024 * 1024;
```

**File:** rs/p2p/quic_transport/src/request_handler.rs (L50-90)
```rust
    let mut inflight_requests: JoinSet<Result<(), P2PError>> = tokio::task::JoinSet::new();
    let mut quic_metrics_scrape = tokio::time::interval(QUIC_METRIC_SCRAPE_INTERVAL);
    // The extreme result of a slow handler is that the stream limit will be reach, hence
    // having buffered up to the stream limit number of messages/requests.
    // A better approach will be to use a router implemented as a tower service and accept
    // streams iff the router is ready. Then the actual number of buffered messages is determined
    // by the handlers instead by the underlying implementation.
    loop {
        tokio::select! {
            Some(completed_request) = inflight_requests.join_next() => {
                match completed_request {
                    Ok(res) => {
                        let _ = res.inspect_err(|err| info!(every_n_seconds => 60, log, "{:?}", err));
                    }
                    Err(err) => {
                        // Cancelling tasks is ok. Panicking tasks are not.
                        if err.is_panic() {
                            std::panic::resume_unwind(err.into_panic());
                        }
                    }
                }
            },
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

**File:** rs/p2p/quic_transport/src/request_handler.rs (L173-178)
```rust
    let request_bytes = recv_stream
        .read_to_end(MAX_MESSAGE_SIZE_BYTES)
        .await
        .inspect_err(|err| {
            observe_read_to_end_error(err, "read_to_end", &metrics.request_handle_errors_total)
        })?;
```

**File:** rs/p2p/quic_transport/src/connection_manager.rs (L71-75)
```rust
const RECEIVE_WINDOW: VarInt = VarInt::from_u32(200_000_000);
const SEND_WINDOW: u64 = 100_000_000;
const STREAM_RECEIVE_WINDOW: VarInt = VarInt::from_u32(4_000_000);
const MAX_CONCURRENT_BIDI_STREAMS: VarInt = VarInt::from_u32(1_000);
const MAX_CONCURRENT_UNI_STREAMS: VarInt = VarInt::from_u32(1_000);
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L40-54)
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
}
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L373-391)
```rust
            Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => {
                empty_slot.insert(new_slot_entry);
                self.metrics
                    .slot_table_new_entry_total
                    .with_label_values(&[peer_id.to_string().as_str()])
                    .inc();
                (true, None)
            }
            Entry::Vacant(_) => {
                self.metrics.slot_table_limit_exceeded_total.inc();
                warn!(
                    self.log,
                    "Peer {} tries to exceed slot limit {}. Dropping slot update",
                    peer_id,
                    self.slot_limit
                );
                (false, None)
            }
        };
```
