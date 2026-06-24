Based on my analysis of the actual production code, here is my finding:

---

### Title
Unbounded Per-Peer Slot Table Growth via `SLOT_TABLE_NO_LIMIT` Allows Byzantine Node to OOM Honest Replicas — (`rs/p2p/consensus_manager/src/receiver.rs`)

### Summary

A Byzantine node that holds a valid subnet membership (authenticated TLS) can send an unbounded number of slot updates with monotonically increasing `SlotNumber` values to honest replicas. Because consensus, certification, and DKG artifact channels are configured with `SLOT_TABLE_NO_LIMIT = usize::MAX`, the per-peer slot table guard is effectively disabled, allowing unbounded memory allocation until OOM.

### Finding Description

The guard in `handle_slot_update_receive` is:

```rust
Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => {
    empty_slot.insert(new_slot_entry);
    ...
}
Entry::Vacant(_) => {
    warn!(..., "Peer {} tries to exceed slot limit {}. Dropping slot update", ...);
    (false, None)
}
``` [1](#0-0) 

When `slot_limit = usize::MAX`, the condition `peer_slot_table_len < usize::MAX` is always true in practice — a `usize` counter can never reach `usize::MAX` before the process runs out of memory. The guard is therefore a no-op for consensus, certifier, and DKG artifact types. [2](#0-1) [3](#0-2) 

The per-peer slot table is `HashMap<NodeId, HashMap<SlotNumber, SlotEntry<...>>>`: [4](#0-3) 

For each new `SlotNumber` with a unique artifact ID, the code also inserts into `active_assembles` and spawns a new tokio task in `artifact_processor_tasks`: [5](#0-4) 

All three data structures — `slot_table`, `active_assembles`, and `artifact_processor_tasks` — grow without bound.

The topology pruning in `handle_topology_update` only removes nodes that **leave** the topology: [6](#0-5) 

A Byzantine node that remains in the subnet is never pruned, so its slot table entries accumulate indefinitely.

### Impact Explanation

- Each slot update with a new `SlotNumber` + unique artifact ID allocates: one `SlotEntry` in the slot table, one entry in `active_assembles`, and one live tokio task.
- Memory grows as O(N) across all three structures.
- OOM crash of the honest replica halts its participation in consensus.
- If multiple honest replicas are targeted simultaneously, subnet-wide consensus finalization stalls.

### Likelihood Explanation

- Precondition: attacker controls one node with valid subnet membership (authenticated TLS). This is within the IC's stated BFT threat model (up to f < n/3 Byzantine nodes).
- No privileged access, governance majority, or key compromise is required.
- The attack is trivially automatable: send a tight loop of slot updates with incrementing `SlotNumber` values.
- The inbound channel has capacity 100 (`tokio::sync::mpsc::channel(100)`), but the event loop drains it continuously, so backpressure does not prevent the attack. [7](#0-6) 

### Recommendation

Replace `SLOT_TABLE_NO_LIMIT` for consensus/certifier/DKG channels with a realistic per-peer bound derived from the maximum number of in-flight artifacts a legitimate node can advertise per protocol round. The existing guard logic is correct; only the limit value needs to be a finite, protocol-justified constant. The ingress channel already demonstrates this pattern with `SLOT_TABLE_LIMIT_INGRESS`. [8](#0-7) 

### Proof of Concept

```rust
// Unit test: send 1_000_000 slot updates from a single peer with SLOT_TABLE_NO_LIMIT
let (mut mgr, _) = ReceiverManagerBuilder::new()
    .with_slot_limit(usize::MAX)  // SLOT_TABLE_NO_LIMIT
    .build();
let cancellation = CancellationToken::new();
for i in 0..1_000_000u64 {
    mgr.handle_slot_update_receive(
        SlotUpdate {
            slot_number: SlotNumber::from(i),
            commit_id: CommitId::from(i),
            update: Update::Id(i),  // unique artifact ID per slot
        },
        NODE_1,
        ConnId::from(1),
        cancellation.clone(),
    );
}
// slot_table[NODE_1].len() == 1_000_000
// active_assembles.len() == 1_000_000
// artifact_processor_tasks.len() == 1_000_000
// Memory usage: unbounded; process OOMs before loop completes at realistic N
```

The existing test `slot_table_limit_exceeded` confirms the guard works with a finite limit, but no test covers the `usize::MAX` case. [9](#0-8)

### Citations

**File:** rs/p2p/consensus_manager/src/receiver.rs (L43-43)
```rust
    let (update_tx, update_rx) = tokio::sync::mpsc::channel(100);
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L187-196)
```rust
    slot_table: HashMap<NodeId, HashMap<SlotNumber, SlotEntry<WireArtifact::Id>>>,
    active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>,

    #[allow(clippy::type_complexity)]
    artifact_processor_tasks: JoinSet<(watch::Receiver<PeerCounter>, WireArtifact::Id)>,

    topology_watcher: watch::Receiver<SubnetTopology>,

    slot_limit: usize,
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L547-558)
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

**File:** rs/replica/setup_ic_network/src/lib.rs (L257-258)
```rust
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_LIMIT_INGRESS)
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
