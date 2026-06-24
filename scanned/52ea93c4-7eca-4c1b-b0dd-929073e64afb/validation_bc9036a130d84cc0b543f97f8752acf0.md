### Title
Cancellation-Unaware `wait_for` in `process_slot_update` Allows Byzantine Peer to Permanently Stall Assemble Task and Leak Unvalidated Pool Entry — (`rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

A single Byzantine peer can advertise an artifact ID and then withhold the slot-deletion update indefinitely. Once `assemble_message` returns `Done` or `Unwanted`, the task falls through the outer `select!` into a plain `peer_rx.wait_for(|p| p.is_empty()).await` that has no cancellation guard. The task blocks forever, `UnvalidatedArtifactMutation::Remove` is never sent, the artifact leaks in the unvalidated pool, and the `active_assembles` entry is never reclaimed. The developers themselves flag this with `// TODO: NET-1774`.

---

### Finding Description

In `process_slot_update`, the outer `select!` races three futures:

```
select! {
    _ = cancellation_token.cancelled() => {}          // arm 1
    assemble_result = assemble_artifact => { … }      // arm 2
    _ = all_peers_deleted_artifact => { … }           // arm 3
}
``` [1](#0-0) 

Once arm 2 wins (assembler returns `Done` or `Unwanted`), execution leaves the `select!` entirely. The cancellation token is no longer polled. The code then unconditionally awaits:

```rust
// TODO: NET-1774
let _ = peer_rx.wait_for(|p| p.is_empty()).await;   // line 500 (Done branch)
// …
let _ = peer_rx.wait_for(|p| p.is_empty()).await;   // line 521 (Unwanted branch)
``` [2](#0-1) 

`peer_rx` is a `watch::Receiver<PeerCounter>`. `wait_for` returns only when the predicate is true (counter empty) **or** when the `watch::Sender` is dropped. The sender lives in `active_assembles`: [3](#0-2) 

The sender is removed from `active_assembles` only inside `handle_artifact_processor_joined` when the task finishes: [4](#0-3) 

This is a circular dependency: the task won't finish until `wait_for` returns; `wait_for` won't return until the sender is dropped; the sender won't be dropped until the task finishes.

**During normal operation**, a Byzantine peer that advertises artifact X and never sends a slot-deletion keeps the peer counter non-empty forever. The task is permanently stuck.

**During shutdown**, `start_event_loop` breaks on cancellation and drops `self`. Struct fields drop in declaration order — `active_assembles` (field 8) drops before `artifact_processor_tasks` (field 9, a `JoinSet`): [5](#0-4) 

Dropping `active_assembles` drops the sender, which wakes `wait_for` with `Err`. But the `JoinSet` is dropped immediately after in the same synchronous drop sequence, aborting the task before it can execute the `Remove` send. So `UnvalidatedArtifactMutation::Remove` is never sent even during graceful shutdown.

The only natural escape hatch is a topology update that removes the Byzantine peer from the subnet: [6](#0-5) 

A Byzantine peer that remains a subnet member (the normal case below the fault threshold) is never pruned this way.

---

### Impact Explanation

Per Byzantine peer, up to `slot_limit` assemble tasks can be permanently stuck. For each stuck task:

- `UnvalidatedArtifactMutation::Remove` is never sent → the artifact leaks in the unvalidated pool for the lifetime of the node process.
- The `active_assembles` entry is never reclaimed → the map entry for that artifact ID is permanently occupied (though new peer advertisements for the same ID are still routed to the existing stuck task via the watch channel, so no second task is spawned).
- Tokio task slots and watch-channel memory are consumed indefinitely.

With `f` Byzantine peers each filling their slot table, the unvalidated pool accumulates up to `f × slot_limit` leaked artifacts. On a 13-node subnet with `f = 4` and a typical `slot_limit`, this is a bounded but persistent memory and resource leak that degrades node performance over time without requiring any privileged access.

---

### Likelihood Explanation

The attacker is a single Byzantine replica peer — an unprivileged protocol participant reachable via the standard P2P slot-update path (`update_handler` → `slot_updates_rx` → `handle_slot_update_receive`). No key material, governance majority, or external infrastructure is required. The peer simply sends one slot-update and then goes silent. The `// TODO: NET-1774` annotation confirms the DFINITY team has already identified this gap. [7](#0-6) [8](#0-7) 

---

### Recommendation

Wrap both `wait_for` calls in a `select!` that also polls `cancellation_token.cancelled()`, and treat cancellation as equivalent to "all peers deleted" (i.e., proceed to send `Remove` and exit):

```rust
select! {
    _ = cancellation_token.cancelled() => {}
    _ = peer_rx.wait_for(|p| p.is_empty()) => {
        let _ = sender.send(UnvalidatedArtifactMutation::Remove(id)).await;
    }
}
```

This is exactly what NET-1774 tracks.

---

### Proof of Concept

State-machine test (no network required):

1. Build a `ConsensusManagerReceiver` with a mock assembler that immediately returns `AssembleResult::Done`.
2. Call `handle_slot_update_receive` for peer A, artifact ID X — this spawns the assemble task.
3. Yield to the runtime so the task runs, sends `Insert`, and reaches `wait_for`.
4. Fire the cancellation token.
5. Assert (with a short timeout) that the task terminates **and** that `UnvalidatedArtifactMutation::Remove(X)` is received on the unvalidated-pool channel.

Under the current code, step 5 times out: the task is aborted by the `JoinSet` drop without ever sending `Remove`, confirming the leak.

### Citations

**File:** rs/p2p/consensus_manager/src/receiver.rs (L186-192)
```rust

    slot_table: HashMap<NodeId, HashMap<SlotNumber, SlotEntry<WireArtifact::Id>>>,
    active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>,

    #[allow(clippy::type_complexity)]
    artifact_processor_tasks: JoinSet<(watch::Receiver<PeerCounter>, WireArtifact::Id)>,

```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L317-319)
```rust
        } else {
            self.active_assembles.remove(&id);
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
