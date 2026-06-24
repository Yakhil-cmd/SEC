### Title
Unbounded `active_assembles` Growth via Malicious Peer Slot Updates Causes Node Memory/CPU Exhaustion — (`File: rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

In `ConsensusManagerReceiver::handle_slot_update_receive`, the `active_assembles` HashMap and `artifact_processor_tasks` JoinSet grow without any size cap. While the per-peer `slot_table` is bounded by a `slot_limit` parameter, most artifact types (consensus, certifier, DKG, IDKG, HTTPS outcalls) are instantiated with `SLOT_TABLE_NO_LIMIT` (effectively `usize::MAX`). A single malicious subnet peer can flood a victim node with slot updates carrying unique artifact IDs, causing unbounded memory and async-task growth until the node becomes unresponsive and is ejected from the subnet.

---

### Finding Description

**Root cause — `active_assembles` has no independent size bound.**

`ConsensusManagerReceiver` maintains two unbounded structures:

```rust
slot_table: HashMap<NodeId, HashMap<SlotNumber, SlotEntry<WireArtifact::Id>>>,
active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>,
artifact_processor_tasks: JoinSet<(watch::Receiver<PeerCounter>, WireArtifact::Id)>,
``` [1](#0-0) 

The per-peer slot table is guarded:

```rust
Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => { ... }
Entry::Vacant(_) => { /* drop */ }
``` [2](#0-1) 

But when `to_add = true` and the artifact ID is not yet in `active_assembles`, a new entry is unconditionally inserted and a new async task is spawned:

```rust
None => {
    self.active_assembles.insert(id.clone(), tx);
    self.artifact_processor_tasks.spawn_on(
        Self::process_slot_update(...),
        &self.rt_handle,
    );
}
``` [3](#0-2) 

There is **no cap on `active_assembles`**. Its size is bounded only by `num_peers × slot_limit`. For all artifact types except ingress, the production setup passes `SLOT_TABLE_NO_LIMIT`: [4](#0-3) [5](#0-4) 

**Entry point — the `update_handler` HTTP route.**

Authenticated subnet peers send slot updates to `/{artifact_name}/update`. The handler decodes the protobuf and forwards it to the event loop with no rate-limiting or ID-space validation:

```rust
async fn update_handler<Artifact: PbArtifact>(
    State((log, sender)): State<(ReplicaLogger, InboundSlotUpdatesSender<Artifact>)>,
    Extension(peer): Extension<NodeId>,
    Extension(conn_id): Extension<ConnId>,
    payload: Bytes,
) -> Result<(), UpdateHandlerError<Artifact>> {
``` [6](#0-5) 

**Attack flow:**

1. Malicious node (authenticated subnet member) opens a QUIC connection to a victim node.
2. It sends a continuous stream of `SlotUpdate` messages, each with a monotonically increasing `slot_id` and a unique `artifact_id`.
3. Each message passes the `slot_limit` guard (since `SLOT_TABLE_NO_LIMIT ≈ usize::MAX`), inserts a new entry into `slot_table`, inserts a new entry into `active_assembles`, and spawns a new Tokio task in `artifact_processor_tasks`.
4. The victim's heap grows without bound; the Tokio runtime is saturated with assembler tasks that each attempt to fetch the (non-existent) artifact from peers.
5. The victim node becomes unresponsive, fails to participate in consensus rounds, and is eventually ejected from the subnet.

**Why `active_assembles` is not self-cleaning:** Entries are only removed when an assembler task finishes *and* no peer is still advertising the ID, or when a topology update removes the advertising peer. A malicious peer that stays in the subnet and keeps advertising unique IDs prevents both cleanup paths. [7](#0-6) [8](#0-7) 

---

### Impact Explanation

A single malicious subnet node (below the consensus fault threshold) can exhaust the memory and CPU of one or more victim nodes by flooding them with slot updates carrying unique artifact IDs. Each update spawns an unbounded async task and grows the `active_assembles` map. The victim node eventually OOMs or becomes too CPU-starved to participate in consensus, causing it to be removed from the active set. Repeating this against multiple nodes can reduce the honest-node count below the fault-tolerance threshold, halting consensus and preventing transaction confirmation — a total network shutdown for the affected subnet.

---

### Likelihood Explanation

Any node that is admitted to a subnet as a validator can immediately execute this attack against any other node in the same subnet. No special privileges, leaked keys, or governance majority are required. The QUIC transport authenticates the peer as a subnet member but does not rate-limit or validate the artifact-ID space of slot updates. The attack is deterministic and requires only a sustained stream of protobuf-encoded messages.

---

### Recommendation

1. **Cap `active_assembles` independently of `slot_limit`.** Introduce a global limit on the number of concurrent assembler tasks (e.g., `MAX_ACTIVE_ASSEMBLES = num_peers × expected_slots_per_peer`). Drop or reject slot updates that would exceed this limit.
2. **Apply `slot_limit` to all artifact types.** Replace `SLOT_TABLE_NO_LIMIT` with a realistic per-peer bound for consensus, DKG, IDKG, certifier, and HTTPS-outcall artifacts, sized to the expected protocol working set.
3. **Rate-limit slot updates per peer.** Enforce a maximum rate of new-ID introductions per peer per second at the `update_handler` layer.
4. **Evict stale assembler tasks.** Add a timeout to assembler tasks so that tasks for IDs that are never fulfilled are cleaned up, preventing accumulation even under non-malicious conditions.

---

### Proof of Concept

```
// Attacker node sends to victim via QUIC /{consensus}/update endpoint:
for slot_id in 0..u64::MAX {
    send SlotUpdate {
        commit_id: slot_id,
        slot_id:   slot_id,
        update:    Update::Id(unique_artifact_id(slot_id)),
    }
}
// Each message passes the slot_limit guard (SLOT_TABLE_NO_LIMIT ≈ usize::MAX),
// inserts into slot_table[attacker_node_id][slot_id],
// inserts into active_assembles[unique_id],
// and spawns a new process_slot_update task.
// Victim heap and task count grow without bound until OOM or scheduler starvation.
```

The `slot_table_limit_exceeded` test in the same file confirms the per-peer guard works when `slot_limit` is finite, but the production `SLOT_TABLE_NO_LIMIT` path is unguarded: [9](#0-8)

### Citations

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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L290-327)
```rust
    pub(crate) fn handle_artifact_processor_joined(
        &mut self,
        peer_rx: watch::Receiver<PeerCounter>,
        id: WireArtifact::Id,
        cancellation_token: CancellationToken,
    ) {
        self.metrics.assemble_task_finished_total.inc();
        // Invariant: Peer sender should only be dropped in this task..
        debug_assert!(peer_rx.has_changed().is_ok());

        // peer advertised after task finished.
        if !peer_rx.borrow().is_empty() {
            self.metrics.assemble_task_restart_after_join_total.inc();
            self.metrics.assemble_task_started_total.inc();
            self.artifact_processor_tasks.spawn_on(
                Self::process_slot_update(
                    self.log.clone(),
                    id,
                    None,
                    peer_rx,
                    self.sender.clone(),
                    self.artifact_assembler.clone(),
                    self.metrics.clone(),
                    cancellation_token.clone(),
                ),
                &self.rt_handle,
            );
        } else {
            self.active_assembles.remove(&id);
        }
        debug_assert!(
            self.slot_table
                .values()
                .flat_map(HashMap::values)
                .all(|v| self.active_assembles.contains_key(&v.id)),
            "Every entry in the slot table should have an active assemble task."
        );
    }
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L373-390)
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
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L399-420)
```rust
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
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L541-568)
```rust
    /// Notifies all running tasks about the topology update.
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

**File:** rs/replica/setup_ic_network/src/lib.rs (L237-247)
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
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L268-280)
```rust
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
```
