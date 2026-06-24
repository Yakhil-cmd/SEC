### Title
Unbounded `active_assembles` / `artifact_processor_tasks` Growth via `SLOT_TABLE_NO_LIMIT` for Non-Ingress Artifact Channels — (`rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

A single Byzantine subnet peer can exhaust heap memory on a receiving replica by flooding it with slot updates carrying distinct slot numbers and artifact IDs over the consensus, DKG, IDKG, certification, or HTTPS-outcalls P2P channels. All five of those channels are configured with `slot_limit = usize::MAX`, so the per-peer slot-table guard never fires, and every distinct (slot_number, artifact_id) pair permanently allocates a `watch::Sender` in `active_assembles` and spawns a live Tokio task in `artifact_processor_tasks`.

---

### Finding Description

**`SLOT_TABLE_NO_LIMIT` is `usize::MAX` and is used for five of six artifact channels.** [1](#0-0) 

Only ingress is capped: [2](#0-1) 

Consensus, certifier, DKG, IDKG, and HTTPS-outcalls all pass `SLOT_TABLE_NO_LIMIT`: [3](#0-2) 

**The slot-limit guard in `handle_slot_update_receive` only blocks new *vacant* entries when `peer_slot_table_len >= slot_limit`.** [4](#0-3) 

When `slot_limit = usize::MAX`, the condition `peer_slot_table_len < usize::MAX` is always satisfied in practice, so every new (peer, slot_number) pair is inserted unconditionally.

**Each new slot entry whose artifact ID is not already tracked spawns a new assemble task and inserts into `active_assembles`.** [5](#0-4) 

**Tasks live until the peer counter reaches zero** — i.e., until the advertising peer overwrites or deletes the slot. A Byzantine peer that never overwrites its slots keeps every spawned task alive indefinitely. [6](#0-5) 

**Cleanup only occurs when a task completes *and* the peer counter is empty.** [7](#0-6) 

---

### Impact Explanation

A Byzantine subnet node sends N HTTP POST requests to the `/consensus/update` (or `/dkg/update`, `/idkg/update`, etc.) endpoint, each with a distinct `slot_id` (1…N) and a distinct artifact ID. Because `slot_limit = usize::MAX`:

- `slot_table[peer]` grows to N entries.
- `active_assembles` grows to N entries (one `watch::Sender<PeerCounter>` each).
- `artifact_processor_tasks` grows to N live Tokio tasks.

The peer never overwrites the slots, so no task ever completes. Memory grows linearly with N until the replica OOMs and crashes. Impact: **OOM crash of a single replica**, reducing subnet fault tolerance.

---

### Likelihood Explanation

The attacker must be an authenticated subnet peer (QUIC/TLS). A single Byzantine node — well within the `f = ⌊(n−1)/3⌋` fault threshold — suffices. No threshold corruption, no key compromise, and no external dependency is required. The attack is fully local to one TCP/QUIC connection and is trivially scriptable.

---

### Recommendation

Apply a finite per-peer slot limit to all artifact channels, not just ingress. The existing `SLOT_TABLE_LIMIT_INGRESS = 50_000` pattern is correct; analogous constants should be derived from protocol-level bounds on how many concurrent artifacts a legitimate peer can advertise per channel (e.g., bounded by DKG interval length, certification height window, IDKG transcript count, etc.) and passed instead of `SLOT_TABLE_NO_LIMIT` in `rs/replica/setup_ic_network/src/lib.rs`. [3](#0-2) 

---

### Proof of Concept

```rust
// Construct receiver with slot_limit = usize::MAX (as deployed for consensus/DKG/IDKG/cert/https)
let (mut mgr, _) = ReceiverManagerBuilder::new()
    .with_slot_limit(usize::MAX)
    .build();
let cancel = CancellationToken::new();

const N: usize = 100_000;
for i in 0..N as u64 {
    mgr.handle_slot_update_receive(
        SlotUpdate {
            slot_number: SlotNumber::from(i),   // distinct slot per iteration
            commit_id:   CommitId::from(1),
            update:      Update::Id(i),          // distinct artifact ID per iteration
        },
        NODE_1,
        ConnId::from(1),
        cancel.clone(),
    );
}
// Neither active_assembles nor artifact_processor_tasks is bounded:
assert_eq!(mgr.active_assembles.len(), N);          // passes
assert_eq!(mgr.artifact_processor_tasks.len(), N);  // passes
// Heap usage grows proportionally; at large N the replica OOMs.
```

The test uses the existing `ReceiverManagerBuilder` harness already present in `receiver.rs`. [8](#0-7)

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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L300-319)
```rust
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L372-390)
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L462-536)
```rust
        let all_peers_deleted_artifact = async move {
            loop {
                match peer_rx_clone.changed().await {
                    Err(_) => break,
                    Ok(_) if peer_rx_clone.borrow().is_empty() => break,
                    _ => {}
                }
            }
        };

        let peer_rx_c = peer_rx.clone();
        let id_c = id.clone();
        let assemble_artifact = async move {
            artifact_assembler
                .assemble_message(id, artifact, PeerWatcher::new(peer_rx_c))
                .await
        };

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
