The code is clear enough to render a verdict. Let me confirm the exact slot-limit guard logic:

### Title
Unbounded Per-Peer Slot Table Growth for IDKG/DKG/Consensus/Certification Channels Allows Single Byzantine Peer to Exhaust Replica Heap Memory — (`rs/replica/setup_ic_network/src/lib.rs`, `rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

IDKG, DKG, consensus, certification, and HTTPS-outcalls artifact channels are registered with `slot_limit = SLOT_TABLE_NO_LIMIT = usize::MAX`, while only ingress is capped at `SLOT_TABLE_LIMIT_INGRESS = 50_000`. The per-peer slot-table guard in `handle_slot_update_receive` is the only admission control for these channels. Because `usize::MAX` is the limit, the guard is never triggered in practice. A single authenticated Byzantine subnet peer can therefore advertise an unbounded number of distinct `(slot_number, artifact_id)` pairs, causing `slot_table`, `active_assembles`, and `artifact_processor_tasks` to grow without bound, exhausting replica heap memory and stalling consensus/IDKG progress.

---

### Finding Description

**Registration of channels with no effective limit**

In `AbortableBroadcastChannels::new`, every non-ingress channel is registered with `SLOT_TABLE_NO_LIMIT`:

```
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
``` [1](#0-0) 

IDKG, DKG, consensus, certification, and HTTPS-outcalls all pass `SLOT_TABLE_NO_LIMIT`: [2](#0-1) 

Only ingress uses the bounded constant: [3](#0-2) 

**The guard that is supposed to protect these channels**

`handle_slot_update_receive` checks the per-peer slot count before inserting a new slot:

```rust
Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => {
    empty_slot.insert(new_slot_entry);
    ...
    (true, None)
}
Entry::Vacant(_) => {
    // drop and warn
    (false, None)
}
``` [4](#0-3) 

When `self.slot_limit = usize::MAX`, the condition `peer_slot_table_len < usize::MAX` is always satisfied for any realistic number of slots, so the guard branch at line 381 is never reached for IDKG/DKG/consensus/certification.

**Unbounded growth of `active_assembles` and `artifact_processor_tasks`**

Every new slot entry whose artifact ID has not been seen before causes:
1. A new entry in `active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>`
2. A new Tokio task spawned into `artifact_processor_tasks: JoinSet<...>` [5](#0-4) 

Each spawned `process_slot_update` task remains alive until the peer removes the slot or disconnects, because it awaits `peer_rx.wait_for(|p| p.is_empty())`: [6](#0-5) 

A connected Byzantine peer that never sends deletions keeps all tasks alive indefinitely.

**Topology-based cleanup does not help against a connected peer**

`handle_topology_update` only prunes slot table entries for peers that have *left* the subnet topology: [7](#0-6) 

A Byzantine peer that remains a subnet member retains all its slot table entries and all associated tasks.

**The data structures that grow**

```rust
slot_table: HashMap<NodeId, HashMap<SlotNumber, SlotEntry<WireArtifact::Id>>>,
active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>,
artifact_processor_tasks: JoinSet<(watch::Receiver<PeerCounter>, WireArtifact::Id)>,
``` [8](#0-7) 

Each of these grows O(N) with the number of distinct slot IDs advertised by the Byzantine peer.

---

### Impact Explanation

- **Memory exhaustion**: Each slot table entry, `active_assembles` entry, and Tokio task consumes heap memory. With N distinct slot IDs, memory grows proportionally to N. A single Byzantine peer can drive N to the point of OOM, crashing the replica process.
- **Consensus/IDKG stall**: The event loop in `start_event_loop` is single-threaded and processes one event at a time. With a large `artifact_processor_tasks` JoinSet, the `join_next()` arm dominates scheduling, starving legitimate slot updates and stalling consensus and IDKG progress.
- **Scope**: Affects all honest replicas in the subnet that are connected to the Byzantine peer.

---

### Likelihood Explanation

- The QUIC transport is TLS-authenticated, so only actual subnet members can send slot updates. However, the IC fault-tolerance model explicitly allows up to f Byzantine nodes (f < n/3). A single Byzantine subnet node is within the stated threat model ("protocol peer behavior below the consensus fault threshold").
- The attack requires no special privileges beyond being a subnet member. The attacker simply sends HTTP POST requests to the IDKG/DKG update endpoint with distinct `slot_id` values in the protobuf payload.
- The `update_handler` accepts any valid protobuf-encoded `SlotUpdate` and forwards it to the event loop with no rate limiting or slot-count pre-check: [9](#0-8) 

- The channel buffer is only 100 messages, but the event loop drains it continuously, so the attacker can sustain a steady stream of new slot IDs.

---

### Recommendation

Apply a per-peer slot limit to IDKG, DKG, consensus, certification, and HTTPS-outcalls channels analogous to the ingress limit. The appropriate bound should reflect the maximum number of legitimate in-flight artifacts a single honest peer would ever advertise for each artifact type (e.g., bounded by the DKG interval length, the number of active IDKG transcripts, etc.). Replace `SLOT_TABLE_NO_LIMIT` with a protocol-derived constant for each channel type, or introduce a single conservative upper bound (e.g., 10,000–100,000) that is still far above any legitimate usage but prevents unbounded growth from a single peer.

---

### Proof of Concept

State-machine test (no network required):

```rust
// Instantiate ConsensusManagerReceiver with slot_limit = usize::MAX (as deployed for IDKG)
let (mut mgr, _) = ReceiverManagerBuilder::new()
    .with_slot_limit(usize::MAX)  // matches production SLOT_TABLE_NO_LIMIT
    .build();
let cancellation = CancellationToken::new();
let peer = NODE_1;

// Byzantine peer sends N distinct slot IDs, each with a unique artifact ID
for i in 0..100_000u64 {
    mgr.handle_slot_update_receive(
        SlotUpdate {
            slot_number: SlotNumber::from(i),       // distinct slot per iteration
            commit_id: CommitId::from(1),
            update: Update::Id(i),                  // distinct artifact ID per iteration
        },
        peer,
        ConnId::from(1),
        cancellation.clone(),
    );
}

// Assert unbounded growth
assert_eq!(mgr.slot_table.get(&peer).unwrap().len(), 100_000);
assert_eq!(mgr.active_assembles.len(), 100_000);
assert_eq!(mgr.artifact_processor_tasks.len(), 100_000);
// RSS growth is proportional to N; repeat with N = 1_000_000 to trigger OOM
```

The existing test `slot_table_limit_exceeded` at line 847 already demonstrates the guard works correctly when `slot_limit = 2`, confirming that setting `slot_limit = usize::MAX` is the root cause — the guard exists but is rendered inert by the chosen constant. [10](#0-9)

### Citations

**File:** rs/replica/setup_ic_network/src/lib.rs (L72-75)
```rust
/// This limit is used to protect against a malicious peer advertising many ingress messages.
/// If no malicious peers are present the ingress pools are bounded by a separate limit.
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L498-522)
```rust
                        // wait for deletion from peers
                        // TODO: NET-1774
                        let _ = peer_rx.wait_for(|p| p.is_empty()).await;

                        // Purge artifact from the unvalidated pool. In theory this channel can get full if there is a bug in
                        // consensus and each round takes very long time. However, the duration of this await is not IO-bound
                        // so for the time being it is fine that sending over the channel is not done as part of a select.
                        if sender.send(UnvalidatedArtifactMutation::Remove(id)).await.is_err() {
                            error!(
                                log,
                                "The receiving side of the channel, owned by the consensus thread, was closed. \
                                This should be an infallible situation since a cancellation token should be received. \
                                If this happens then most likely there is a very serious synchonization bug."
                            );
                        }
                        metrics
                            .assemble_task_result_total
                            .with_label_values(&[ASSEMBLE_TASK_RESULT_COMPLETED])
                            .inc();
                    }
                    AssembleResult::Unwanted => {
                        // wait for deletion from peers
                        // TODO: NET-1774
                        let _ = peer_rx.wait_for(|p| p.is_empty()).await;
                        metrics
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L542-568)
```rust
    fn handle_topology_update(&mut self) {
        self.metrics.topology_updates_total.inc();
        let new_topology = self.topology_watcher.borrow().clone();
        let mut nodes_leaving_topology = HashSet::new();

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

        for peers_sender in self.active_assembles.values() {
            peers_sender
                .send_if_modified(|set| nodes_leaving_topology.iter().any(|n| set.remove(*n)));
        }
        debug_assert!(
            self.slot_table.len() <= self.topology_watcher.borrow().iter().count(),
            "Slot table contains more nodes than nodes in subnet after pruning"
        );
    }
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
