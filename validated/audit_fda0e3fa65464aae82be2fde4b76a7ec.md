Audit Report

## Title
Unbounded `active_assembles` Growth via Malicious Peer Slot Updates Causes Node Memory/CPU Exhaustion — (File: rs/p2p/consensus_manager/src/receiver.rs)

## Summary
`ConsensusManagerReceiver` maintains an unbounded `active_assembles` HashMap and `artifact_processor_tasks` JoinSet. The per-peer slot table guard is gated on `slot_limit`, but all artifact types except ingress are instantiated with `SLOT_TABLE_NO_LIMIT = usize::MAX`, rendering the guard ineffective. A single authenticated subnet peer can flood a victim node with slot updates carrying unique slot IDs and artifact IDs, causing unbounded heap growth and Tokio task accumulation until the node OOMs or becomes too CPU-starved to participate in consensus.

## Finding Description

**Root cause — `SLOT_TABLE_NO_LIMIT` disables the only guard protecting `active_assembles`.**

`SLOT_TABLE_NO_LIMIT` is defined as `usize::MAX`: [1](#0-0) 

It is passed for every artifact type except ingress: [2](#0-1) [3](#0-2) [4](#0-3) 

The per-peer slot table guard in `handle_slot_update_receive` checks: [5](#0-4) 

With `slot_limit = usize::MAX`, the condition `peer_slot_table_len < usize::MAX` is always true in practice (a `usize` counter cannot reach `usize::MAX` before the system OOMs), so the `Entry::Vacant(_)` drop arm is never reached.

When `to_add = true` and the artifact ID is not yet in `active_assembles`, a new entry is unconditionally inserted and a new async task is spawned with no cap: [6](#0-5) 

The two unbounded structures are: [7](#0-6) 

**Why tasks do not self-clean:** Each spawned `process_slot_update` task waits for `peer_rx.wait_for(|p| p.is_empty())` before completing. As long as the malicious peer keeps the slot active (i.e., keeps advertising the artifact ID), the `PeerCounter` never reaches zero and the task never returns. The `handle_artifact_processor_joined` cleanup path only removes an entry from `active_assembles` when the task finishes *and* the peer counter is empty: [8](#0-7) 

The topology-update cleanup path only helps if the malicious peer is removed from the subnet topology: [9](#0-8) 

**Attack flow:**
1. Malicious authenticated subnet peer opens a QUIC connection to a victim node.
2. It sends a continuous stream of `SlotUpdate` messages, each with a monotonically increasing `slot_id` and a unique `artifact_id`.
3. Each message passes the `slot_limit` guard (since `usize::MAX` is never exceeded before OOM), inserts a new entry into `slot_table[attacker][slot_id]`, inserts a new entry into `active_assembles[unique_id]`, and spawns a new Tokio task.
4. The victim's heap grows without bound; the Tokio runtime accumulates assembler tasks that each block waiting for the malicious peer to retract the advertisement.
5. The victim node OOMs or becomes too CPU-starved to participate in consensus rounds.

The `slot_table_limit_exceeded` test confirms the guard works when `slot_limit` is finite, but the production `SLOT_TABLE_NO_LIMIT` path is unguarded: [10](#0-9) 

## Impact Explanation
A single malicious subnet node (below the consensus fault threshold) can exhaust the memory and CPU of one or more victim nodes by flooding them with valid protocol-level slot updates. This is not a raw volumetric DDoS — each message causes persistent state growth (a HashMap entry plus a live Tokio task) that accumulates until the victim OOMs or is scheduler-starved. The victim node fails to participate in consensus and is eventually ejected. Repeating this against multiple nodes can reduce the honest-node count below the fault-tolerance threshold, halting consensus and blocking transaction confirmation for the affected subnet. This matches the allowed High impact: **application/platform-level DoS, consensus blocking, or subnet availability impact not based on raw volumetric DDoS** ($2,000–$10,000).

## Likelihood Explanation
Any node admitted to a subnet as a validator can immediately execute this attack against any peer in the same subnet. No special privileges, leaked keys, or governance majority are required. The QUIC transport authenticates the peer as a subnet member but applies no rate-limiting or artifact-ID-space validation at the `update_handler` layer. The attack is deterministic: a sustained stream of protobuf-encoded `SlotUpdate` messages with incrementing `slot_id` and unique `artifact_id` fields is sufficient. The attacker need not be above the Byzantine fault threshold.

## Recommendation
1. **Cap `active_assembles` independently of `slot_limit`.** Introduce a global limit on concurrent assembler tasks (e.g., `num_peers × expected_slots_per_peer`). Drop slot updates that would exceed this limit.
2. **Apply a realistic `slot_limit` to all artifact types.** Replace `SLOT_TABLE_NO_LIMIT` with a per-peer bound sized to the expected protocol working set for consensus, DKG, IDKG, certifier, and HTTPS-outcall artifacts.
3. **Rate-limit new-ID introductions per peer.** Enforce a maximum rate of unique artifact-ID introductions per peer per second at the `update_handler` or event-loop layer.
4. **Add assembler task timeouts.** Evict assembler tasks for IDs that are never fulfilled within a protocol-appropriate deadline, preventing accumulation even under non-malicious conditions.

## Proof of Concept
The existing `slot_table_limit_exceeded` unit test (with `slot_limit = 2`) demonstrates that the guard works when finite. The following extension demonstrates the unguarded production path:

```rust
// In a test using ReceiverManagerBuilder::new() (slot_limit defaults to usize::MAX):
let (mut mgr, _channels) = ReceiverManagerBuilder::new().build(); // slot_limit = usize::MAX
let cancellation = CancellationToken::new();
for i in 0u64..10_000 {
    mgr.handle_slot_update_receive(
        SlotUpdate {
            slot_number: SlotNumber::from(i),
            commit_id: CommitId::from(i),
            update: Update::Id(i),          // unique artifact ID each iteration
        },
        NODE_1,
        ConnId::from(1),
        cancellation.clone(),
    );
}
// With slot_limit = usize::MAX, all 10_000 entries are accepted:
assert_eq!(mgr.active_assembles.len(), 10_000);
assert_eq!(mgr.artifact_processor_tasks.len(), 10_000);
// In production, this loop runs until the node OOMs.
```

This mirrors the production configuration for consensus, DKG, IDKG, certifier, and HTTPS-outcall artifact types, all of which pass `SLOT_TABLE_NO_LIMIT`.

### Citations

**File:** rs/replica/setup_ic_network/src/lib.rs (L74-75)
```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
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

**File:** rs/replica/setup_ic_network/src/lib.rs (L291-303)
```rust
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L187-195)
```rust
    slot_table: HashMap<NodeId, HashMap<SlotNumber, SlotEntry<WireArtifact::Id>>>,
    active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>,

    #[allow(clippy::type_complexity)]
    artifact_processor_tasks: JoinSet<(watch::Receiver<PeerCounter>, WireArtifact::Id)>,

    topology_watcher: watch::Receiver<SubnetTopology>,

    slot_limit: usize,
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L547-563)
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
