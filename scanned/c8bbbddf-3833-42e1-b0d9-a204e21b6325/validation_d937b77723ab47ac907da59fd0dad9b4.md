The code is fully readable. Let me trace the exact execution path for the claimed scenario.

**`PeerCounter` is a reference count, not a set:** [1](#0-0) 

`insert` increments the `u32` counter for an existing `NodeId` and returns `false` (not newly inserted). `remove` decrements by exactly 1.

**`handle_slot_update_receive` calls `insert(peer_id)` for every new slot entry pointing to the same artifact ID:** [2](#0-1) 

When a slot is vacant and below `slot_limit`, `to_add = true` is set, and `h.insert(peer_id)` is called on the shared `PeerCounter` — even if `peer_id` is already present. `send_if_modified` still modifies the counter (increments it) even when the closure returns `false`.

**`process_slot_update` blocks on `peer_rx.wait_for(|p| p.is_empty())` before sending `Remove`:** [3](#0-2) 

**`handle_topology_update` calls `remove` only once per departing peer, not once per slot:** [4](#0-3) 

---

### Title
Byzantine Peer Inflates `PeerCounter` via Multi-Slot Same-ID Advertisement, Permanently Blocking Unvalidated Pool Cleanup — (`rs/p2p/consensus_manager/src/receiver.rs`)

### Summary

`PeerCounter` is a `HashMap<NodeId, u32>` reference count. A Byzantine peer can advertise the same artifact ID on N distinct slot numbers (up to `slot_limit`), causing the counter for that peer to reach N. If the peer withholds even one retraction, the counter never reaches zero, `process_slot_update` blocks forever on `peer_rx.wait_for(|p| p.is_empty())`, and the artifact is never removed from the unvalidated pool.

### Finding Description

In `handle_slot_update_receive`, when a new slot entry is accepted (`to_add = true`) and the artifact ID already has an active assemble task, the code calls:

```rust
sender.send_if_modified(|h| h.insert(peer_id));
```

`PeerCounter::insert` for an already-present `NodeId` increments the `u32` counter and returns `false`. `send_if_modified` still applies the mutation regardless of the return value — it only skips the watcher notification. So the counter is incremented to 2, 3, …, N for each additional slot the same peer uses to advertise the same artifact ID.

Retraction happens when a slot is overwritten: `sender.send_if_modified(|h| h.remove(peer_id))`. `PeerCounter::remove` decrements by exactly 1. If the peer advertised on 3 slots and only retracts 2, the counter stays at 1 permanently.

`process_slot_update` then blocks indefinitely at:

```rust
let _ = peer_rx.wait_for(|p| p.is_empty()).await;
```

The `UnvalidatedArtifactMutation::Remove` message is never sent, the artifact is never purged from the unvalidated pool, and the assemble task leaks.

The topology-update path (`handle_topology_update`) also only calls `remove` once per departing peer regardless of how many slots that peer occupied with the same ID, so it does not fix the inflated counter either.

### Impact Explanation

- The artifact is permanently retained in the unvalidated pool on every honest receiving node.
- The `process_slot_update` Tokio task is permanently blocked, leaking a task and the associated `watch` channel.
- A Byzantine peer can repeat this for many artifact IDs (up to `slot_limit` slots per artifact, across many artifacts), causing unbounded memory growth and task accumulation — a targeted DoS against the P2P receive path of honest subnet nodes.

### Likelihood Explanation

A single Byzantine subnet node (below the fault threshold) can execute this attack without any privileged access. The slot update messages are accepted from any authenticated peer in the subnet topology. No threshold corruption, key compromise, or external dependency is required.

### Recommendation

`PeerCounter` should track membership as a **set** (i.e., `HashSet<NodeId>`), not a reference count. A single peer advertising the same artifact ID on multiple slots should not increment the counter beyond 1. The invariant should be: the counter for a `NodeId` equals 1 if and only if at least one slot in the slot table for that peer maps to this artifact ID, and 0 otherwise. Alternatively, before calling `insert`, check whether the peer already has a nonzero count and skip the increment.

### Proof of Concept

```rust
// Byzantine peer advertises artifact ID 0 on 3 slots
mgr.handle_slot_update_receive(SlotUpdate { slot_number: 1.into(), commit_id: 1.into(), update: Update::Id(0) }, BYZANTINE_PEER, ConnId::from(1), cancel.clone());
mgr.handle_slot_update_receive(SlotUpdate { slot_number: 2.into(), commit_id: 2.into(), update: Update::Id(0) }, BYZANTINE_PEER, ConnId::from(1), cancel.clone());
mgr.handle_slot_update_receive(SlotUpdate { slot_number: 3.into(), commit_id: 3.into(), update: Update::Id(0) }, BYZANTINE_PEER, ConnId::from(1), cancel.clone());
// PeerCounter for BYZANTINE_PEER is now 3

// Retract only 2 of the 3 slots
mgr.handle_slot_update_receive(SlotUpdate { slot_number: 1.into(), commit_id: 4.into(), update: Update::Id(99) }, BYZANTINE_PEER, ConnId::from(1), cancel.clone());
mgr.handle_slot_update_receive(SlotUpdate { slot_number: 2.into(), commit_id: 5.into(), update: Update::Id(99) }, BYZANTINE_PEER, ConnId::from(1), cancel.clone());
// PeerCounter for BYZANTINE_PEER is now 1 — never reaches 0

// Assert: after assembly completes, Remove is never sent to the unvalidated pool
tokio::time::timeout(Duration::from_secs(5), channels.unvalidated_artifact_receiver.recv())
    .await
    .expect_err("Remove should never arrive — artifact is stuck");
```

### Citations

**File:** rs/p2p/consensus_manager/src/receiver.rs (L123-169)
```rust
#[derive(Debug)]
pub struct PeerCounter(HashMap<NodeId, u32>);

impl PeerCounter {
    pub(crate) fn new() -> Self {
        Self(HashMap::new())
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn peers(&self) -> impl Iterator<Item = &NodeId> {
        self.0.keys()
    }

    /// Returns true if value is newly inserted
    pub(crate) fn insert(&mut self, node: NodeId) -> bool {
        match self.0.entry(node) {
            Entry::Occupied(mut entry) => {
                *entry.get_mut() += 1;
                false
            }
            Entry::Vacant(entry) => {
                entry.insert(1);
                true
            }
        }
    }

    /// Returns true if removed key was present and counter got to zero
    pub(crate) fn remove(&mut self, node: NodeId) -> bool {
        match self.0.entry(node) {
            Entry::Occupied(mut entry) => {
                assert!(*entry.get() != 0);

                if *entry.get() == 1 {
                    entry.remove();
                    true
                } else {
                    *entry.get_mut() -= 1;
                    false
                }
            }
            Entry::Vacant(_) => false,
        }
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L560-563)
```rust
        for peers_sender in self.active_assembles.values() {
            peers_sender
                .send_if_modified(|set| nodes_leaving_topology.iter().any(|n| set.remove(*n)));
        }
```
