### Title
Unvalidated Pool Artifact Leak via Indefinitely Blocked `wait_for` in `process_slot_update` — (`rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

A Byzantine peer below the consensus fault threshold can advertise an artifact, cause it to be assembled and inserted into the unvalidated pool via `UnvalidatedArtifactMutation::Insert`, and then permanently withhold the slot overwrite that would decrement the `PeerCounter` to zero. Because the subsequent `peer_rx.wait_for(|p| p.is_empty()).await` at line 500 is **not** wrapped in a `select!` with the cancellation token, the `process_slot_update` task blocks indefinitely, `UnvalidatedArtifactMutation::Remove` is never emitted, and the artifact remains in the unvalidated pool for the lifetime of the node process. The developers themselves have flagged this exact call site with `// TODO: NET-1774`.

---

### Finding Description

**Entry point**: A Byzantine replica peer (below the f < n/3 fault threshold) sends a `SlotUpdate` message over the P2P transport layer.

**Trace**:

1. `update_handler` receives the wire message and forwards it to `handle_slot_update_receive`. [1](#0-0) 

2. `handle_slot_update_receive` creates a fresh `PeerCounter`, inserts the Byzantine peer's `NodeId`, and spawns a `process_slot_update` task. [2](#0-1) 

3. Inside `process_slot_update`, a `select!` races three futures: cancellation, `assemble_artifact`, and `all_peers_deleted_artifact`. If `assemble_artifact` resolves first with `AssembleResult::Done` (the normal push path), the code sends `UnvalidatedArtifactMutation::Insert` and then unconditionally awaits:

```rust
// wait for deletion from peers
// TODO: NET-1774
let _ = peer_rx.wait_for(|p| p.is_empty()).await;
``` [3](#0-2) 

   This `wait_for` is **outside** the `select!` and therefore **ignores the cancellation token entirely**.

4. The `PeerCounter` is only decremented in two places:
   - `handle_slot_update_receive` when a slot overwrite arrives (`sender.send_if_modified(|h| h.remove(peer_id))`). [4](#0-3) 
   - `handle_topology_update` when a peer leaves the subnet topology. [5](#0-4) 

5. A Byzantine peer below the fault threshold is never removed from the subnet topology. If it simply stops sending slot updates for the advertised slot, the `PeerCounter` never reaches zero, `wait_for` never returns `Ok`, and `UnvalidatedArtifactMutation::Remove` is never sent. [6](#0-5) 

**Circular deadlock on shutdown**: The `watch::Sender` (`tx`) for the artifact lives in `active_assembles`. It is only removed in `handle_artifact_processor_joined`, which is only called after the task joins. The task will not join until `wait_for` returns. `wait_for` returns `Err` only when the sender is dropped. The sender is not dropped until the task joins. This is a true circular dependency; the only escape is process termination. [7](#0-6) 

**Slot limit**: For consensus, certifier, DKG, and HTTPS-outcall artifact types, the production configuration passes `SLOT_TABLE_NO_LIMIT` (effectively `usize::MAX`), so there is no per-peer cap on the number of stuck artifacts. [8](#0-7) 

---

### Impact Explanation

- **Unvalidated pool memory leak**: Every artifact inserted via `Insert` but never followed by `Remove` accumulates in the unvalidated pool for the lifetime of the node process.
- **Task and channel handle leak**: Each stuck `process_slot_update` task holds a live entry in `active_assembles` and `artifact_processor_tasks`, both of which grow without bound.
- **No consensus correctness impact**: Consensus will validate or invalidate the artifact normally; the leak is purely a resource exhaustion issue.
- **Scope**: Affects all artifact types using `SLOT_TABLE_NO_LIMIT` (consensus, certification, DKG, iDKG, HTTPS outcalls). Ingress is partially bounded by `SLOT_TABLE_LIMIT_INGRESS` but still leaks one artifact per slot per Byzantine peer.

---

### Likelihood Explanation

- The attacker only needs to be a single Byzantine replica in the subnet (below the f < n/3 threshold), which is explicitly within the IC threat model.
- The attack requires no cryptographic capability: simply send one `SlotUpdate` with the artifact payload and then go silent on that slot.
- The `// TODO: NET-1774` comment at both `wait_for` call sites confirms the DFINITY team is aware this is unresolved. [9](#0-8) [10](#0-9) 

---

### Recommendation

Wrap both `peer_rx.wait_for(|p| p.is_empty()).await` calls in a `select!` that also listens on the cancellation token, so that the task can be aborted on shutdown and the `Remove` mutation is still emitted (or the pool is flushed via a separate cleanup path). Additionally, consider adding a timeout or a maximum artifact lifetime in the unvalidated pool to bound the leak even when the cancellation token is not fired.

---

### Proof of Concept

State-machine test (no network required):

1. Construct a `ConsensusManagerReceiver` with a mock assembler that returns `AssembleResult::Done` immediately.
2. Call `handle_slot_update_receive` for `NODE_1` on slot 1 with artifact `A`.
3. Await `UnvalidatedArtifactMutation::Insert` — confirm it arrives.
4. Do **not** send any further slot update for `NODE_1` / slot 1.
5. Assert that within a generous timeout (e.g., 5 seconds), `UnvalidatedArtifactMutation::Remove` is **never** received.
6. Assert that `mgr.active_assembles` still contains the entry for artifact `A`.

This directly mirrors the existing test harness already present in the file: [11](#0-10)

### Citations

**File:** rs/p2p/consensus_manager/src/receiver.rs (L79-120)
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L424-440)
```rust
        if let Some(to_remove) = to_remove {
            match self.active_assembles.get_mut(&to_remove) {
                Some(sender) => {
                    sender.send_if_modified(|h| h.remove(peer_id));
                    self.metrics.slot_table_removals_total.inc();
                }
                None => {
                    error!(
                        self.log,
                        "Slot table contains an artifact ID that is not present in the `active_assembles`. This should never happen."
                    );
                    if cfg!(debug_assertions) {
                        panic!("Invariant violated");
                    }
                }
            };
        }
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L498-512)
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
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L520-521)
```rust
                        // TODO: NET-1774
                        let _ = peer_rx.wait_for(|p| p.is_empty()).await;
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L560-563)
```rust
        for peers_sender in self.active_assembles.values() {
            peers_sender
                .send_if_modified(|set| nodes_leaving_topology.iter().any(|n| set.remove(*n)));
        }
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L603-637)
```rust
#[cfg(test)]
mod tests {
    use std::{convert::Infallible, time::Duration};

    use axum::{body::Body, http::Request};
    use ic_logger::replica_logger::no_op_logger;
    use ic_metrics::MetricsRegistry;
    use ic_p2p_test_utils::{consensus::U64Artifact, mocks::MockArtifactAssembler};
    use ic_test_utilities_logger::with_test_replica_logger;
    use ic_types::{RegistryVersion, artifact::IdentifiableArtifact};
    use ic_types_test_utils::ids::{NODE_1, NODE_2};
    use tokio::time::timeout;
    use tower::util::ServiceExt;

    use super::*;

    const PROCESS_ARTIFACT_TIMEOUT: Duration = Duration::from_millis(1000);

    struct ReceiverManagerBuilder {
        // Slot updates received from peers
        slot_updates_rx: Receiver<(SlotUpdate<U64Artifact>, NodeId, ConnId)>,
        sender: Sender<UnvalidatedArtifactMutation<U64Artifact>>,
        artifact_assembler: MockArtifactAssembler,
        topology_watcher: watch::Receiver<SubnetTopology>,
        slot_limit: usize,

        channels: Channels,
    }

    type ConsensusManagerReceiverForTest =
        ConsensusManagerReceiver<U64Artifact, U64Artifact, MockArtifactAssembler>;

    struct Channels {
        unvalidated_artifact_receiver: Receiver<UnvalidatedArtifactMutation<U64Artifact>>,
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
