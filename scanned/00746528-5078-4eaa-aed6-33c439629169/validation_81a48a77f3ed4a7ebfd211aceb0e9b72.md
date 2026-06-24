Based on my investigation of the code, here is my assessment:

---

### Title
Unbounded Tokio Task and Memory Growth via `SLOT_TABLE_NO_LIMIT` in P2P Consensus Manager Receiver — (`rs/p2p/consensus_manager/src/receiver.rs`)

### Summary

A single Byzantine peer below the consensus fault threshold can exhaust honest replica memory and Tokio task scheduler capacity by sending an unbounded number of slot updates with distinct slot numbers and distinct artifact IDs on channels configured with `SLOT_TABLE_NO_LIMIT` (`usize::MAX`). The per-peer slot limit guard exists in code but is rendered ineffective for all non-ingress artifact channels.

### Finding Description

**`SLOT_TABLE_NO_LIMIT` definition:**

`rs/replica/setup_ic_network/src/lib.rs` defines:

```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
``` [1](#0-0) 

The comment explicitly states the ingress limit protects against malicious peers. No equivalent protection is applied to consensus, certifier, DKG, IDKG, or HTTPS outcalls channels — all of which use `SLOT_TABLE_NO_LIMIT`.

**The guard in `handle_slot_update_receive`:**

```rust
// Only insert slot update if we are below peer slot table limit.
Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => {
    empty_slot.insert(new_slot_entry);
    ...
    (true, None)
}
Entry::Vacant(_) => {
    // warn and drop
    (false, None)
}
``` [2](#0-1) 

When `self.slot_limit = usize::MAX`, the condition `peer_slot_table_len < usize::MAX` is always true (a `usize` counter cannot reach `usize::MAX` before OOM), so the guard never fires.

**Unbounded task spawning:**

Every new `(peer_id, slot_number)` pair with a previously-unseen artifact ID that passes the slot limit check causes:

1. A new entry in `slot_table[peer_id]`
2. A new entry in `active_assembles`
3. A new Tokio task spawned in `artifact_processor_tasks` [3](#0-2) 

**Tasks do not self-terminate for fake IDs:**

Each spawned `process_slot_update` task runs until one of three conditions:
- Cancellation token fires (replica shutdown)
- `assemble_message` completes
- All peers delete the artifact (peer counter goes to zero) [4](#0-3) 

If the Byzantine peer sends N distinct IDs on N distinct slot numbers and never overwrites any slot, the peer counter for each ID stays non-empty indefinitely. The `all_peers_deleted_artifact` branch only fires when the slot is overwritten or the peer is removed from topology. A Byzantine peer trivially avoids both by using monotonically increasing slot numbers.

**The `slot_table_limit_exceeded` test confirms the guard works when a finite limit is set:** [5](#0-4) 

The test uses `slot_limit: 2` and confirms the third slot is dropped. With `usize::MAX`, this test would show unbounded growth.

### Impact Explanation

A single Byzantine peer (below the f < n/3 fault threshold) can:
- Grow `active_assembles` and `artifact_processor_tasks` to an arbitrary size
- Exhaust process heap memory (each `HashMap` entry + `watch` channel + `JoinSet` task has overhead)
- Exhaust Tokio task scheduler capacity (each spawned task consumes scheduler resources)
- Cause honest replicas to crash or become unresponsive, halting subnet progress

This violates the core IC invariant that a single Byzantine peer cannot exhaust honest replica resources.

### Likelihood Explanation

The attack is straightforward: a Byzantine node sends `SlotUpdate` protobuf messages over the existing QUIC transport connection with monotonically increasing `slot_id` values and distinct artifact IDs. No special privileges are required — any subnet member can send slot updates to peers. The attack is local-testable with a state-machine test as described in the question.

### Recommendation

Apply a finite, protocol-appropriate per-peer slot limit to all artifact channels, not just ingress. The existing guard infrastructure is correct; only the limit value needs to change. For consensus, DKG, IDKG, and HTTPS outcalls, the natural protocol bounds on concurrent artifacts per peer should inform the limit (e.g., a few thousand at most). The `SLOT_TABLE_NO_LIMIT` constant should be removed or restricted to contexts where it is genuinely safe.

### Proof of Concept

State-machine test outline:
1. Construct `ConsensusManagerReceiver` with `slot_limit = usize::MAX`
2. From a single `peer_id`, call `handle_slot_update_receive` N=100,000 times, each with a distinct `slot_number` (0..N) and distinct artifact ID (0..N)
3. Assert `mgr.active_assembles.len() == N` and `mgr.artifact_processor_tasks.len() == N`
4. Confirm no slot updates were dropped (no `slot_table_limit_exceeded_total` metric increments)

The existing test `slot_table_limit_exceeded` at line 847 of `receiver.rs` demonstrates the guard works with a finite limit — the same test with `slot_limit = usize::MAX` would show unbounded growth. [6](#0-5)

### Citations

**File:** rs/replica/setup_ic_network/src/lib.rs (L72-75)
```rust
/// This limit is used to protect against a malicious peer advertising many ingress messages.
/// If no malicious peers are present the ingress pools are bounded by a separate limit.
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L172-196)
```rust
pub(crate) struct ConsensusManagerReceiver<
    Artifact: IdentifiableArtifact,
    WireArtifact: IdentifiableArtifact + PbArtifact,
    Assembler,
> {
    log: ReplicaLogger,
    metrics: ConsensusManagerMetrics,
    rt_handle: Handle,

    // Receive side:
    slot_updates_rx: Receiver<(SlotUpdate<WireArtifact>, NodeId, ConnId)>,
    sender: Sender<UnvalidatedArtifactMutation<Artifact>>,

    artifact_assembler: Assembler,

    slot_table: HashMap<NodeId, HashMap<SlotNumber, SlotEntry<WireArtifact::Id>>>,
    active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>,

    #[allow(clippy::type_complexity)]
    artifact_processor_tasks: JoinSet<(watch::Receiver<PeerCounter>, WireArtifact::Id)>,

    topology_watcher: watch::Receiver<SubnetTopology>,

    slot_limit: usize,
}
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L393-420)
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
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L480-536)
```rust
        select! {
            _ = cancellation_token.cancelled() => {}
            assemble_result = assemble_artifact => {
                match assemble_result {
                    AssembleResult::Done { message, peer_id } => {
                        let id = message.id();
                        // Sends artifact to the pool. In theory this channel can get full if there is a bug in consensus
                        // and each round takes very long time. However, the duration of this await is not IO-bound so for
                        // the time being it is fine that sending over the channel is not done as part of a select.
                        if sender.send(UnvalidatedArtifactMutation::Insert((message, peer_id))).await.is_err() {
                            error!(
                                log,
                                "The receiving side of the channel, owned by the consensus thread, was closed. \
                                This should be an infallible situation since a cancellation token should be received. \
                                If this happens then most likely there is a very serious synchonization bug."
                            );
                        }

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
                            .assemble_task_result_total
                            .with_label_values(&[ASSEMBLE_TASK_RESULT_DROP])
                            .inc();

                    },
                }
            }
            _ = all_peers_deleted_artifact => {
                metrics
                    .assemble_task_result_total
                    .with_label_values(&[ASSEMBLE_TASK_RESULT_ALL_PEERS_DELETED])
                    .inc();
            },
        };
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
