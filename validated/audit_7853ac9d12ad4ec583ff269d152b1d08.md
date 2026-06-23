### Title
Indefinite `wait_for` Block After `Insert` Prevents `Remove` Emission, Leaking Unvalidated Pool Memory — (`rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

After `assemble_message` returns `AssembleResult::Done`, `process_slot_update` sends `UnvalidatedArtifactMutation::Insert` and then unconditionally awaits `peer_rx.wait_for(|p| p.is_empty())` **outside any `select!` with the cancellation token**. A Byzantine peer below the fault threshold that advertises an artifact and never overwrites that slot keeps the `PeerCounter` non-zero indefinitely, blocking the `wait_for` forever and preventing `UnvalidatedArtifactMutation::Remove` from ever being emitted. The artifact is permanently retained in the unvalidated pool for the lifetime of the node process, and the spawned tokio task is permanently leaked.

---

### Finding Description

In `process_slot_update` the outer `select!` has three arms: cancellation, `assemble_artifact`, and `all_peers_deleted_artifact`. [1](#0-0) 

When `assemble_artifact` wins the race (arm 2), the other two futures — including `cancellation_token.cancelled()` — are **dropped**. Execution then falls into sequential code: [2](#0-1) 

The `wait_for` at line 500 (and its twin at line 521 for `Unwanted`) is **not** wrapped in any new `select!` with the cancellation token: [3](#0-2) 

The `// TODO: NET-1774` comment on both calls is an explicit developer acknowledgement that this is a known deficiency.

`peer_rx` is a `watch::Receiver<PeerCounter>`. `wait_for` returns `Err` only when the corresponding `watch::Sender` is dropped. That sender lives in `active_assembles`: [4](#0-3) 

The sender is removed from `active_assembles` only inside `handle_artifact_processor_joined`, which is called only when the task **finishes**: [5](#0-4) 

This is a circular dependency: the task cannot finish until `wait_for` returns; `wait_for` returns only when the sender is dropped or the counter reaches zero; the sender is dropped only when the task finishes.

The peer counter is decremented in exactly two places:
1. A slot overwrite from the same peer (`handle_slot_update_receive` line 427).
2. A topology update removing the peer (`handle_topology_update` line 562). [6](#0-5) [7](#0-6) 

A Byzantine peer that stays connected and never sends another slot update for the occupied slot triggers neither path.

---

### Impact Explanation

- **Unvalidated pool memory leak**: `UnvalidatedArtifactMutation::Remove` is never sent, so the artifact inserted via `Insert` is never purged from the unvalidated pool by the P2P layer. The pool grows without bound (bounded only by `slot_limit` per peer).
- **Tokio task leak**: Each stuck `process_slot_update` task holds a live tokio task and a `watch::Receiver`, consuming runtime resources indefinitely.
- **Amplification**: With `f` Byzantine peers (below the BFT fault threshold), each filling `slot_limit` slots, the total stuck artifacts scale as `f × slot_limit`. Large or expensive artifacts (consensus blocks, state-sync chunks) amplify memory pressure.
- **Potential OOM / replica crash**: Sustained accumulation can exhaust heap memory on the replica node, causing it to crash and drop out of consensus, degrading subnet liveness.

---

### Likelihood Explanation

The attack requires only a single subnet peer (a Byzantine replica below the fault threshold) to:
1. Connect to the victim node (normal protocol behavior).
2. Send one slot update advertising an artifact.
3. Never send another slot update for that slot.

No cryptographic material, admin access, or majority corruption is needed. The peer simply goes silent on that slot. This is trivially achievable by any Byzantine node in the subnet.

---

### Recommendation

Wrap the post-assembly `wait_for` in a `select!` that also listens to the cancellation token, and consider adding a timeout:

```rust
select! {
    _ = cancellation_token.cancelled() => { return (peer_rx, id_c); }
    result = peer_rx.wait_for(|p| p.is_empty()) => { let _ = result; }
}
```

Additionally, track the referenced `NET-1774` ticket to resolution and add a bounded timeout so that even a non-cancelled stuck task eventually unblocks and emits `Remove`.

---

### Proof of Concept

State-machine test (no network required):

1. Create a `ConsensusManagerReceiver` with a mock assembler that returns `AssembleResult::Done` immediately.
2. Call `handle_slot_update_receive` for `NODE_1`, slot 1, artifact ID 0.
3. Observe `UnvalidatedArtifactMutation::Insert` arrives on the channel.
4. Do **not** send any further slot update (no overwrite, no topology change).
5. Assert that `UnvalidatedArtifactMutation::Remove` is **never** received within a generous timeout (e.g., 5 seconds).
6. Assert that `artifact_processor_tasks.len() == 1` (task is still alive and blocked).

This matches the existing test harness pattern already present in the file: [8](#0-7) 

The existing `overwrite_slot_send_remove` test demonstrates that `Remove` **is** emitted when a slot overwrite occurs. The inverse — no overwrite, no `Remove` — is the exploit scenario and is not currently tested or guarded against.

### Citations

**File:** rs/p2p/consensus_manager/src/receiver.rs (L188-188)
```rust
    active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>,
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L290-319)
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L560-563)
```rust
        for peers_sender in self.active_assembles.values() {
            peers_sender
                .send_if_modified(|set| nodes_leaving_topology.iter().any(|n| set.remove(*n)));
        }
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L976-1025)
```rust
    async fn overwrite_slot_send_remove() {
        fn make_artifact_assembler() -> MockArtifactAssembler {
            let mut artifact_assembler = MockArtifactAssembler::default();
            artifact_assembler
                .expect_assemble_message()
                .returning(|id, _, _: PeerWatcher| {
                    Box::pin(async move {
                        AssembleResult::Done {
                            message: U64Artifact::id_to_msg(id, 100),
                            peer_id: NODE_1,
                        }
                    })
                });
            artifact_assembler
        }
        let (mut mgr, mut channels) = ReceiverManagerBuilder::new()
            .with_artifact_assembler_maker(make_artifact_assembler)
            .build();
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
        // Verify that slot updates is correctly inserted into slot table.
        assert_eq!(
            mgr.slot_table
                .get(&NODE_1)
                .unwrap()
                .get(&SlotNumber::from(1))
                .unwrap(),
            &SlotEntry {
                conn_id: ConnId::from(1),
                commit_id: CommitId::from(1),
                id: 0,
            }
        );
        assert_eq!(mgr.slot_table.len(), 1);
        assert_eq!(mgr.slot_table.get(&NODE_1).unwrap().len(), 1);
        assert_eq!(mgr.active_assembles.len(), 1);
        assert_eq!(mgr.artifact_processor_tasks.len(), 1);
        assert_eq!(
            channels.unvalidated_artifact_receiver.recv().await.unwrap(),
            UnvalidatedArtifactMutation::Insert((U64Artifact::id_to_msg(0, 100), NODE_1))
        );
```
