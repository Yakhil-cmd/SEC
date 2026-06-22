### Title
Byzantine Peer Can Permanently Block Assemble Tasks via Missing Slot-Deletion — (`rs/p2p/consensus_manager/src/receiver.rs`)

### Summary

In `process_slot_update`, after `assemble_artifact` resolves with `Done` or `Unwanted`, execution falls into the body of the `select!` arm and calls `peer_rx.wait_for(|p| p.is_empty()).await` at lines 500 and 521. These awaits are **inside the selected arm's body**, not in a new `select!`, so the cancellation token is no longer polled. A single Byzantine subnet peer that advertises an artifact and never sends a slot-deletion update will hold these awaits indefinitely, permanently blocking the assemble task, preventing `UnvalidatedArtifactMutation::Remove` from being sent, and keeping the `active_assembles` entry alive forever. The developers have explicitly flagged this with `// TODO: NET-1774`.

---

### Finding Description

The `process_slot_update` function races three futures in a `select!`:

```
select! {
    _ = cancellation_token.cancelled() => {}          // branch A
    assemble_result = assemble_artifact => { ... }    // branch B
    _ = all_peers_deleted_artifact => { ... }         // branch C
}
``` [1](#0-0) 

Once branch B wins (the assembler returns `Done` or `Unwanted`), Tokio executes the arm body to completion. The `select!` is no longer active, so branch A (cancellation) is never polled again. Inside that arm body:

- **Line 500** (`Done` path): `let _ = peer_rx.wait_for(|p| p.is_empty()).await;`
- **Line 521** (`Unwanted` path): `let _ = peer_rx.wait_for(|p| p.is_empty()).await;` [2](#0-1) 

Both are bare `.await`s with no cancellation guard. The `PeerCounter` for artifact X is only decremented when the event loop receives a slot-deletion from the advertising peer (via `send_if_modified(|h| h.remove(peer_id))` in `handle_slot_update_receive`). [3](#0-2) 

The `watch::Sender` held in `active_assembles` is only dropped in `handle_artifact_processor_joined` when the task finishes. Since the task is stuck, the sender is never dropped, so `wait_for` never receives a `RecvError` to break out. [4](#0-3) 

The only external mitigation is `handle_topology_update`, which removes a peer from all `PeerCounter`s if it leaves the subnet topology. But a Byzantine peer that remains a valid subnet member is never removed from the topology. [5](#0-4) 

For consensus artifacts, the production `slot_limit` is `SLOT_TABLE_NO_LIMIT = usize::MAX`, so there is no per-peer cap on how many artifact IDs a Byzantine peer can advertise. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

1. **Permanent task leak**: Each artifact ID advertised by the Byzantine peer and assembled before deletion results in one permanently stuck Tokio task.
2. **Unvalidated pool pollution**: For the `Done` path, `UnvalidatedArtifactMutation::Remove` is never sent, so the assembled artifact remains in the unvalidated pool indefinitely.
3. **`active_assembles` map exhaustion**: The entry for the artifact ID is never removed, so no new assemble task for the same ID can ever be started (new slot updates from honest peers just call `send_if_modified` on the existing sender, which the stuck task is waiting on).
4. **Shutdown hang**: When the cancellation token fires during node shutdown, stuck tasks do not terminate, potentially blocking the `JoinSet` drain in `start_event_loop`. [8](#0-7) 

---

### Likelihood Explanation

The attacker is a single Byzantine subnet peer — within the standard BFT fault tolerance of `f < n/3`. The attack requires only:
1. Sending one slot-update HTTP request to the victim node's P2P endpoint.
2. Waiting for the assembler to return `Done` or `Unwanted`.
3. Never sending a slot-deletion.

No threshold corruption, no key material, no privileged access is required. The `// TODO: NET-1774` comment confirms the developers are aware the `wait_for` calls lack cancellation protection. [9](#0-8) [10](#0-9) 

---

### Recommendation

Wrap the `wait_for` calls in a `select!` that also polls `cancellation_token.cancelled()`, and send `Remove` (or skip it) on cancellation. For example:

```rust
select! {
    _ = cancellation_token.cancelled() => {
        // optionally still send Remove to clean up
    }
    _ = peer_rx.wait_for(|p| p.is_empty()) => {}
}
```

This is exactly what NET-1774 tracks.

---

### Proof of Concept

State-machine test (no network required):

1. Build a `ConsensusManagerReceiver` with a mock assembler that immediately returns `AssembleResult::Done`.
2. Call `handle_slot_update_receive` from `NODE_1` for artifact ID `X` — this spawns the assemble task.
3. Await `Insert` on the unvalidated channel (confirms assembler returned `Done` and task reached line 500).
4. Fire the cancellation token.
5. With a timeout, attempt to join the assemble task from `artifact_processor_tasks` — **the join never completes** because the task is stuck at `wait_for`.
6. Assert that `Remove` is never received on the unvalidated channel.

The existing test `overwrite_slot_send_remove` already demonstrates the normal path where a slot-overwrite triggers peer removal and unblocks `wait_for`. The Byzantine scenario is the same setup but without the overwrite/deletion step. [11](#0-10)

### Citations

**File:** rs/p2p/consensus_manager/src/receiver.rs (L236-288)
```rust
    async fn start_event_loop(mut self, cancellation_token: CancellationToken) {
        loop {
            select! {
                _ = cancellation_token.cancelled() => {
                    error!(
                        self.log,
                        "Sender event loop for the P2P client `{:?}` terminated. \
                        No more slot updates will be sent for this client.",
                        uri_prefix::<WireArtifact>()
                    );
                    break;
                }
                Some(result) = self.artifact_processor_tasks.join_next() => {
                    match result {
                        Ok((receiver, id)) => {
                            self.handle_artifact_processor_joined(receiver, id, cancellation_token.clone());

                        }
                        Err(err) => {
                            // If the task panics we propagate the panic. But we allow tasks to be canceled.
                            // Task can be cancelled if someone calls .abort()
                            if err.is_panic() {
                                std::panic::resume_unwind(err.into_panic());
                            }
                        }
                    }
                }
                Some((slot_update, peer_id, conn_id)) = self.slot_updates_rx.recv() => {
                    self.handle_slot_update_receive(slot_update, peer_id, conn_id, cancellation_token.clone());
                }
                Ok(()) = self.topology_watcher.changed() => {
                    self.handle_topology_update();
                }
            }
            debug_assert_eq!(
                self.active_assembles.len(),
                self.artifact_processor_tasks.len(),
                "Number of artifact processing tasks differs from the available number of channels that \
                communicate with the processing tasks"
            );
            debug_assert!(
                self.artifact_processor_tasks.len()
                    >= HashSet::<WireArtifact::Id>::from_iter(
                        self.slot_table
                            .values()
                            .flat_map(HashMap::values)
                            .map(|s| s.id.clone())
                    )
                    .len(),
                "Number of assemble tasks should always be the same or exceed the number of distinct ids stored."
            );
        }
    }
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L976-1073)
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

        // Send slot update with higher conn id.
        mgr.handle_slot_update_receive(
            SlotUpdate {
                slot_number: SlotNumber::from(1),
                commit_id: CommitId::from(0),
                update: Update::Id(1),
            },
            NODE_1,
            ConnId::from(2),
            cancellation.clone(),
        );
        // Verify that slot table now only contains newer entry.
        assert_eq!(
            mgr.slot_table
                .get(&NODE_1)
                .unwrap()
                .get(&SlotNumber::from(1))
                .unwrap(),
            &SlotEntry {
                conn_id: ConnId::from(2),
                commit_id: CommitId::from(0),
                id: 1,
            }
        );
        assert_eq!(mgr.slot_table.len(), 1);
        assert_eq!(mgr.slot_table.get(&NODE_1).unwrap().len(), 1);

        let joined_artifact_processor = mgr.artifact_processor_tasks.join_next().await;
        let result = joined_artifact_processor
            .expect("Joining artifact processor task failed")
            .expect("Artifact processor task panicked");
        let receiver_unvalidated_1 = channels.unvalidated_artifact_receiver.recv().await.unwrap();
        let receiver_unvalidated_2 = channels.unvalidated_artifact_receiver.recv().await.unwrap();
        assert!(
            (receiver_unvalidated_1
                == UnvalidatedArtifactMutation::Insert((U64Artifact::id_to_msg(1, 100), NODE_1))
                && receiver_unvalidated_2 == UnvalidatedArtifactMutation::Remove(0))
                || (receiver_unvalidated_2
                    == UnvalidatedArtifactMutation::Insert((
                        U64Artifact::id_to_msg(1, 100),
                        NODE_1
                    ))
                    && receiver_unvalidated_1 == UnvalidatedArtifactMutation::Remove(0))
        );

        // Check that assemble task for first slot closes.
        assert_eq!(result.1, 0);
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L74-75)
```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L237-246)
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
```
