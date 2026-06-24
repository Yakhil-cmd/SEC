Let me read the remaining part of receiver.rs and check the topology update handler more carefully.

### Title
Byzantine Peer Multi-Slot Same-ID Inflation Permanently Leaks Artifacts in Unvalidated Pool — (`rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

`PeerCounter` is a reference-counted `HashMap<NodeId, u32>`, not a set. A Byzantine peer can advertise the same artifact ID on N distinct slot numbers, inflating its per-peer counter to N. When the peer leaves the subnet topology, `handle_topology_update` removes all of the peer's slots from the `slot_table` in one pass but calls `PeerCounter::remove` exactly once per peer — decrementing the counter to N-1. The assemble task then waits forever for the counter to reach zero, permanently leaking the artifact in the unvalidated pool and keeping the assemble task alive.

---

### Finding Description

**`PeerCounter` is a refcount, not a set.** [1](#0-0) 

`insert` increments the `u32` for a `NodeId` (or inserts 1 if absent); `remove` decrements by 1 and only removes the entry when the count reaches 1. [2](#0-1) [3](#0-2) 

**Each new vacant slot for the same artifact ID increments the counter.**

In `handle_slot_update_receive`, every time a peer's slot update lands on a previously-unseen slot number (vacant entry, within `slot_limit`), `to_add = true` is set and `sender.send_if_modified(|h| h.insert(peer_id))` is called on the shared `active_assembles` entry for that artifact ID. [4](#0-3) [5](#0-4) 

A Byzantine peer advertising artifact ID `X` on slots 1, 2, 3 causes three `insert(peer_id)` calls → `PeerCounter[peer] = 3`.

**`handle_topology_update` removes all slots but decrements the counter only once.**

When the peer leaves the topology, `slot_table.retain` drops the peer's entire slot map in one call. Then, for every active assemble, `set.remove(*n)` is called exactly once per departing peer: [6](#0-5) 

`nodes_leaving_topology` is a `HashSet<NodeId>`, so the peer appears at most once. `PeerCounter::remove` decrements by 1. Counter goes from 3 → 2. The entry is not removed.

**The assemble task waits forever.**

`process_slot_update` only sends `UnvalidatedArtifactMutation::Remove` after `peer_rx.wait_for(|p| p.is_empty())` resolves. With the counter stuck at 2, this future never completes. [7](#0-6) 

---

### Impact Explanation

- The artifact is permanently retained in the unvalidated pool; the `Remove` mutation is never sent.
- The `process_slot_update` Tokio task is permanently blocked, leaking a task and its associated `watch` channel.
- A single Byzantine peer can repeat this for every artifact type it can produce, across all `slot_limit` slots, multiplying the leak.
- Over time this causes unbounded memory growth in the unvalidated pool and the task set, degrading or halting the replica.

---

### Likelihood Explanation

- Requires only one Byzantine subnet peer (well below the fault threshold).
- The peer simply sends the same artifact ID in slot updates for N different slot numbers before disconnecting or being evicted from the topology.
- No cryptographic material, admin access, or majority collusion is needed.
- The `slot_limit` is the only bound on N; with the default threshold of 30,000 slots, the counter can be inflated substantially. [8](#0-7) 

---

### Recommendation

Replace `PeerCounter::remove` in `handle_topology_update` with a full eviction: instead of decrementing by 1, remove the `NodeId` entry entirely (regardless of its count) when a peer leaves the topology. Alternatively, change `handle_topology_update` to look up how many slots the departing peer held for each artifact ID and call `remove` that many times, or restructure `PeerCounter` to be a `HashSet<NodeId>` (set semantics) and track slot-level multiplicity separately in the slot table.

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut mgr = ReceiverManagerBuilder::new().with_slot_limit(10).build();
let cancellation = CancellationToken::new();

// Byzantine peer advertises artifact ID 42 on 3 different slots
for slot in [1u64, 2, 3] {
    mgr.handle_slot_update_receive(
        SlotUpdate { slot_number: SlotNumber::from(slot), commit_id: CommitId::from(slot), update: Update::Id(42) },
        NODE_1, ConnId::from(1), cancellation.clone(),
    );
}
// PeerCounter for artifact 42: { NODE_1: 3 }

// Peer leaves topology
let (topology_tx, topology_rx) = watch::channel(SubnetTopology::default()); // empty topology
mgr.topology_watcher = topology_rx;
mgr.handle_topology_update();
// PeerCounter for artifact 42: { NODE_1: 2 }  ← NOT empty

// Assert: active_assembles still contains artifact 42 (counter != 0)
// Assert: after waiting PROCESS_ARTIFACT_TIMEOUT, no Remove mutation is sent
// → artifact permanently stuck in unvalidated pool
```

### Citations

**File:** rs/p2p/consensus_manager/src/receiver.rs (L124-124)
```rust
pub struct PeerCounter(HashMap<NodeId, u32>);
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L140-150)
```rust
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
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L154-168)
```rust
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
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L373-379)
```rust
            Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => {
                empty_slot.insert(new_slot_entry);
                self.metrics
                    .slot_table_new_entry_total
                    .with_label_values(&[peer_id.to_string().as_str()])
                    .inc();
                (true, None)
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L393-398)
```rust
        if to_add {
            match self.active_assembles.get(&id) {
                Some(sender) => {
                    self.metrics.slot_table_seen_id_total.inc();
                    sender.send_if_modified(|h| h.insert(peer_id));
                }
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L499-506)
```rust
                        // TODO: NET-1774
                        let _ = peer_rx.wait_for(|p| p.is_empty()).await;

                        // Purge artifact from the unvalidated pool. In theory this channel can get full if there is a bug in
                        // consensus and each round takes very long time. However, the duration of this await is not IO-bound
                        // so for the time being it is fine that sending over the channel is not done as part of a select.
                        if sender.send(UnvalidatedArtifactMutation::Remove(id)).await.is_err() {
                            error!(
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L547-562)
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

        for peers_sender in self.active_assembles.values() {
            peers_sender
                .send_if_modified(|set| nodes_leaving_topology.iter().any(|n| set.remove(*n)));
```

**File:** rs/p2p/consensus_manager/src/sender.rs (L40-40)
```rust
const SLOT_TABLE_THRESHOLD: u64 = 30_000;
```
