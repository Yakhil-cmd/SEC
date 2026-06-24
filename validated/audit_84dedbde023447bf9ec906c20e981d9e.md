Audit Report

## Title
Unbounded Per-Peer Slot Table Growth via `SLOT_TABLE_NO_LIMIT = usize::MAX` Allows Byzantine Subnet Peer to Exhaust Replica Heap Memory — (`rs/p2p/consensus_manager/src/receiver.rs`, `rs/replica/setup_ic_network/src/lib.rs`)

## Summary

All artifact channels except ingress are registered with `slot_limit = usize::MAX`, rendering the per-peer slot-table admission guard permanently inert for consensus, IDKG, DKG, certification, and HTTPS-outcalls channels. A single authenticated Byzantine subnet peer can advertise an unbounded number of distinct `(slot_number, artifact_id)` pairs, causing `slot_table`, `active_assembles`, and `artifact_processor_tasks` to grow without bound, exhausting replica heap memory and crashing or stalling the replica process.

## Finding Description

**Root cause — constants:**
`SLOT_TABLE_NO_LIMIT = usize::MAX` is defined at [1](#0-0)  and passed to every non-ingress channel registration. Ingress alone uses the bounded `SLOT_TABLE_LIMIT_INGRESS = 50_000` at [2](#0-1) , while consensus, certifier, DKG, IDKG, and HTTPS-outcalls all pass `SLOT_TABLE_NO_LIMIT` at [3](#0-2) .

**The inert guard:**
`handle_slot_update_receive` checks `peer_slot_table_len < self.slot_limit` before inserting a new vacant slot entry. [4](#0-3)  With `self.slot_limit = usize::MAX`, the condition is always true for any realistic slot count, so the `Entry::Vacant(_)` drop branch at line 381 is never reached for these channels.

**Unbounded data structure growth:**
Every new slot entry whose artifact ID has not been seen before inserts into `active_assembles` and spawns a new Tokio task into `artifact_processor_tasks`. [5](#0-4)  The three structures that grow O(N) with distinct advertised slot IDs are `slot_table`, `active_assembles`, and `artifact_processor_tasks`. [6](#0-5) 

**Tasks remain alive indefinitely:**
Each spawned `process_slot_update` task awaits `peer_rx.wait_for(|p| p.is_empty())`, blocking until the peer explicitly removes the slot or disconnects. [7](#0-6)  A Byzantine peer that never sends deletions keeps all tasks alive.

**Topology cleanup does not help:**
`handle_topology_update` only prunes entries for peers that have left the subnet topology. [8](#0-7)  A Byzantine peer that remains a subnet member retains all slot table entries and associated tasks indefinitely.

**No rate limiting in the HTTP handler:**
`update_handler` decodes the protobuf payload and forwards it to the event loop with no slot-count pre-check or rate limiting. [9](#0-8) 

## Impact Explanation

A single Byzantine subnet peer (within the f < n/3 fault-tolerance model) can drive heap memory consumption to OOM by advertising N distinct `(slot_number, artifact_id)` pairs across any of the unlimited channels. Each entry consumes heap for the slot table entry, a `watch::Sender<PeerCounter>` in `active_assembles`, and a live Tokio task with its stack and associated state. At sufficient N this crashes the replica process. Even short of OOM, the `join_next()` arm of the event loop is dominated by the large `JoinSet`, starving legitimate slot updates and stalling consensus and IDKG progress. This matches the **High** impact class: application/platform-level DoS, consensus blocking, and subnet availability impact not based on raw volumetric DDoS.

## Likelihood Explanation

The QUIC transport is TLS-authenticated, so only actual subnet members can send slot updates — the attacker must be a Byzantine subnet node. The IC fault-tolerance model explicitly permits up to f < n/3 Byzantine nodes, making this a within-threat-model attacker. No special privileges beyond subnet membership are required. The attacker simply sends repeated HTTP POST requests to the IDKG/DKG/consensus update endpoint with distinct `slot_id` values in the protobuf payload. The existing test `slot_table_limit_exceeded` at [10](#0-9)  confirms the guard mechanism is correct when `slot_limit = 2`, proving that `usize::MAX` is the sole reason the guard is inert in production.

## Recommendation

Replace `SLOT_TABLE_NO_LIMIT` with protocol-derived per-channel bounds for consensus, IDKG, DKG, certification, and HTTPS-outcalls channels, analogous to `SLOT_TABLE_LIMIT_INGRESS`. Each bound should reflect the maximum number of legitimate in-flight artifacts a single honest peer would advertise (e.g., bounded by DKG interval length, number of active IDKG transcripts, consensus round depth). At minimum, introduce a single conservative upper bound (e.g., 10,000–100,000) that is well above any legitimate usage but prevents unbounded growth. The constant `SLOT_TABLE_NO_LIMIT` should be removed or replaced entirely.

## Proof of Concept

The existing unit test infrastructure in `receiver.rs` supports a direct state-machine test with no network required:

```rust
// Instantiate ConsensusManagerReceiver with slot_limit = usize::MAX (production value for IDKG)
let (mut mgr, _channels) = ReceiverManagerBuilder::new()
    .with_slot_limit(usize::MAX)
    .build();
let cancellation = CancellationToken::new();

for i in 0..100_000u64 {
    mgr.handle_slot_update_receive(
        SlotUpdate {
            slot_number: SlotNumber::from(i),
            commit_id: CommitId::from(1),
            update: Update::Id(i),
        },
        NODE_1,
        ConnId::from(1),
        cancellation.clone(),
    );
}

assert_eq!(mgr.slot_table.get(&NODE_1).unwrap().len(), 100_000);
assert_eq!(mgr.active_assembles.len(), 100_000);
assert_eq!(mgr.artifact_processor_tasks.len(), 100_000);
```

This mirrors the pattern of the existing `slot_table_limit_exceeded` test, which already proves the guard works at `slot_limit = 2`. Repeating with N = 1,000,000 will demonstrate proportional RSS growth toward OOM.

### Citations

**File:** rs/replica/setup_ic_network/src/lib.rs (L74-75)
```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L237-303)
```rust
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
        } else {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                consensus_pool.clone(),
                bouncers.consensus,
                metrics_registry.clone(),
            );
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
        };

        let ingress = {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                artifact_pools.ingress_pool.clone(),
                bouncers.ingress,
                metrics_registry.clone(),
            );
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_LIMIT_INGRESS)
        };

        let certifier = {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                artifact_pools.certification_pool.clone(),
                bouncers.certifier,
                metrics_registry.clone(),
            );
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
        };

        let dkg = {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                artifact_pools.dkg_pool.clone(),
                bouncers.dkg,
                metrics_registry.clone(),
            );
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
        };

        let idkg = {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                artifact_pools.idkg_pool.clone(),
                bouncers.idkg,
                metrics_registry.clone(),
            );

            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
        };

        let https_outcalls = {
            let assembler = ic_artifact_downloader::FetchArtifact::new(
                log.clone(),
                rt_handle.clone(),
                artifact_pools.https_outcalls_pool.clone(),
                bouncers.https_outcalls,
                metrics_registry.clone(),
            );

            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_NO_LIMIT)
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L187-195)
```rust
    slot_table: HashMap<NodeId, HashMap<SlotNumber, SlotEntry<WireArtifact::Id>>>,
    active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>,

    #[allow(clippy::type_complexity)]
    artifact_processor_tasks: JoinSet<(watch::Receiver<PeerCounter>, WireArtifact::Id)>,

    topology_watcher: watch::Receiver<SubnetTopology>,

    slot_limit: usize,
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L372-391)
```rust
            // Only insert slot update if we are below peer slot table limit.
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L393-421)
```rust
        if to_add {
            match self.active_assembles.get(&id) {
                Some(sender) => {
                    self.metrics.slot_table_seen_id_total.inc();
                    sender.send_if_modified(|h| h.insert(peer_id));
                }
                None => {
                    self.metrics.assemble_task_started_total.inc();

                    let peer_counter = PeerCounter::new();
                    let (tx, rx) = watch::channel(peer_counter);
                    tx.send_if_modified(|h| h.insert(peer_id));
                    self.active_assembles.insert(id.clone(), tx);

                    self.artifact_processor_tasks.spawn_on(
                        Self::process_slot_update(
                            self.log.clone(),
                            id.clone(),
                            artifact.map(|a| (a, peer_id)),
                            rx,
                            self.sender.clone(),
                            self.artifact_assembler.clone(),
                            self.metrics.clone(),
                            cancellation_token.clone(),
                        ),
                        &self.rt_handle,
                    );
                }
            }
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L498-500)
```rust
                        // wait for deletion from peers
                        // TODO: NET-1774
                        let _ = peer_rx.wait_for(|p| p.is_empty()).await;
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L547-558)
```rust
        self.slot_table.retain(|node_id, _| {
            if !new_topology.is_member(node_id) {
                nodes_leaving_topology.insert(*node_id);
                let _ = self
                    .metrics
                    .slot_table_new_entry_total
                    .remove_label_values(&[node_id.to_string().as_str()]);
                false
            } else {
                true
            }
        });
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L846-887)
```rust
    #[tokio::test]
    async fn slot_table_limit_exceeded() {
        let (mut mgr, _channels) = ReceiverManagerBuilder::new().with_slot_limit(2).build();
        let cancellation = CancellationToken::new();

        mgr.handle_slot_update_receive(
            SlotUpdate {
                slot_number: SlotNumber::from(1),
                commit_id: CommitId::from(1),
                update: Update::Id(0),
            },
            NODE_1,
            ConnId::from(1),
            cancellation.clone(),
        );
        mgr.handle_slot_update_receive(
            SlotUpdate {
                slot_number: SlotNumber::from(2),
                commit_id: CommitId::from(2),
                update: Update::Id(1),
            },
            NODE_1,
            ConnId::from(1),
            cancellation.clone(),
        );
        assert_eq!(mgr.slot_table.len(), 1);
        assert_eq!(mgr.slot_table.get(&NODE_1).unwrap().len(), 2);
        assert_eq!(mgr.active_assembles.len(), 2);
        // Send slot update that exceeds limit
        mgr.handle_slot_update_receive(
            SlotUpdate {
                slot_number: SlotNumber::from(3),
                commit_id: CommitId::from(3),
                update: Update::Id(2),
            },
            NODE_1,
            ConnId::from(1),
            cancellation.clone(),
        );
        assert_eq!(mgr.slot_table.get(&NODE_1).unwrap().len(), 2);
        assert_eq!(mgr.active_assembles.len(), 2);
    }
```
