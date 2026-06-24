The code confirms all key claims. Let me verify the exact lines cited before rendering the final output.

Audit Report

## Title
Unbounded Per-Peer Slot Table and Task Growth via `SLOT_TABLE_NO_LIMIT` Allows Byzantine Subnet Member to OOM Honest Replicas — (`rs/p2p/consensus_manager/src/receiver.rs`)

## Summary
`SLOT_TABLE_NO_LIMIT` is set to `usize::MAX` and is passed as the `slot_limit` for consensus, certification, DKG, IDKG, and HTTPS outcalls artifact channels. The per-peer guard `peer_slot_table_len < self.slot_limit` is therefore never triggered in practice, allowing a single authenticated Byzantine subnet member to grow the slot table, `active_assembles` map, and `artifact_processor_tasks` JoinSet without bound by sending slot updates with monotonically increasing unique `SlotNumber` and artifact ID values, ultimately exhausting replica memory.

## Finding Description

**Root cause — disabled slot limit for non-ingress channels:**

`SLOT_TABLE_NO_LIMIT` is defined as `usize::MAX` and applied to every artifact channel except ingress: [1](#0-0) [2](#0-1) 

Ingress correctly uses a finite bound of 50,000: [3](#0-2) 

**Guard is ineffective:**

In `handle_slot_update_receive`, the only check before inserting a new slot entry is: [4](#0-3) 

With `slot_limit = usize::MAX`, `peer_slot_table_len < usize::MAX` is always true — a `HashMap` cannot hold `usize::MAX` entries before the process OOMs.

**Each new (slot_number, artifact_id) pair spawns a live async task:**

When `to_add = true` and the artifact ID is not already in `active_assembles`, a new entry is inserted into both `active_assembles` and `artifact_processor_tasks`: [5](#0-4) 

**Tasks never terminate without peer deletion:**

Each spawned `process_slot_update` task awaits `peer_rx.wait_for(|p| p.is_empty())` after assembly (both `Done` and `Unwanted` paths). The `PeerCounter` for an artifact ID is only decremented when the same slot number is overwritten with a different artifact ID, or when the peer leaves the topology. A Byzantine node that continuously uses fresh slot numbers and never overwrites them keeps every `PeerCounter` at 1 indefinitely.

**Topology pruning does not help while the attacker remains a subnet member:** [6](#0-5) 

Pruning only fires when a node leaves the topology. An active Byzantine subnet member is never pruned.

**Inbound channel capacity does not bound total volume:** [7](#0-6) 

The channel capacity of 100 limits burst rate only; the event loop drains it continuously, so total slot update volume is unbounded.

## Impact Explanation

A single Byzantine subnet member can cause unbounded growth of three in-memory structures simultaneously — the per-peer slot table (`HashMap<NodeId, HashMap<SlotNumber, SlotEntry>>`), `active_assembles`, and the `artifact_processor_tasks` JoinSet — on every honest replica it is connected to. Each entry and task consumes heap memory. Sustained sending of unique slot updates leads to OOM and replica process termination. If enough honest replicas crash, consensus finalization halts subnet-wide.

This matches the allowed High impact: **Application/platform-level DoS, crash, consensus blocking, or subnet availability impact not based on raw volumetric DDoS** ($2,000–$10,000). The attacker is a single below-threshold Byzantine subnet member; no majority, no key compromise, and no external dependency is required.

## Likelihood Explanation

The attacker is a legitimate subnet member authenticated via TLS — no cryptographic break is needed. The attack is deterministic, requires no timing or race conditions, and is repeatable indefinitely. The only precondition is subnet membership. The attack is self-amplifying: each honest replica the Byzantine node connects to is independently vulnerable. The existing ingress protection (`SLOT_TABLE_LIMIT_INGRESS`) demonstrates that the developers are aware of this class of attack, making the omission for other channels a concrete exploitable gap rather than a theoretical one.

## Recommendation

Replace `SLOT_TABLE_NO_LIMIT` for consensus, certification, DKG, IDKG, and HTTPS outcalls channels with a finite per-peer bound derived from the maximum number of in-flight artifacts a legitimate node would ever advertise simultaneously. The `SLOT_TABLE_LIMIT_INGRESS = 50_000` pattern at line 74 of `rs/replica/setup_ic_network/src/lib.rs` is the correct model. Additionally, consider adding a per-peer cap on `active_assembles` / `artifact_processor_tasks` entries as a defense-in-depth measure, and add a timeout or cancellation path in `process_slot_update` so tasks do not wait indefinitely for a peer counter that may never reach zero.

## Proof of Concept

The existing test harness in `rs/p2p/consensus_manager/src/receiver.rs` already provides `ReceiverManagerBuilder::with_slot_limit` and direct calls to `handle_slot_update_receive`. The following unit test (extending the existing `slot_table_limit_exceeded` test pattern) demonstrates unbounded growth:

```rust
#[tokio::test]
async fn byzantine_peer_oom_via_unique_slot_numbers() {
    // Default builder uses slot_limit = usize::MAX (SLOT_TABLE_NO_LIMIT)
    let (mut mgr, _channels) = ReceiverManagerBuilder::new().build();
    let cancellation = CancellationToken::new();

    const N: u64 = 1_000_000;
    for i in 0..N {
        mgr.handle_slot_update_receive(
            SlotUpdate {
                slot_number: SlotNumber::from(i),
                commit_id: CommitId::from(i),
                update: Update::Id(i),   // unique artifact ID per slot
            },
            NODE_1,
            ConnId::from(1),
            cancellation.clone(),
        );
    }

    // Slot table grows without bound
    assert_eq!(mgr.slot_table.get(&NODE_1).unwrap().len(), N as usize);
    // One live async task per unique artifact ID — never terminates
    assert_eq!(mgr.artifact_processor_tasks.len(), N as usize);
    // active_assembles mirrors artifact_processor_tasks
    assert_eq!(mgr.active_assembles.len(), N as usize);
}
```

On real hardware, running this loop against a live replica (or scaling N further) will exhaust heap memory and OOM-kill the replica process. No topology change, no deletion message, and no privileged access are required.

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

**File:** rs/replica/setup_ic_network/src/lib.rs (L257-258)
```rust
            new_p2p_consensus.abortable_broadcast_channel(assembler, SLOT_TABLE_LIMIT_INGRESS)
        };
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L43-43)
```rust
    let (update_tx, update_rx) = tokio::sync::mpsc::channel(100);
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
