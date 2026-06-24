Now I have all the information needed. Let me verify the `process_slot_update` task behavior to confirm tasks don't self-terminate quickly.

### Title
Unbounded Per-Peer Slot Table Growth via `SLOT_TABLE_NO_LIMIT = usize::MAX` Enables OOM Crash on Honest Replicas — (`rs/replica/setup_ic_network/src/lib.rs` / `rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

A single authenticated Byzantine subnet peer can advertise an unbounded stream of distinct slot updates for consensus, certifier, DKG, iDKG, and HTTPS-outcalls artifact types. Because `SLOT_TABLE_NO_LIMIT = usize::MAX` is used as the per-peer slot table cap for all of these types, the guard in `handle_slot_update_receive` is effectively disabled. Each distinct `(slot_number, artifact_id)` pair inserts a permanent entry into `active_assembles` and spawns a long-lived tokio task in `artifact_processor_tasks`. With no eviction until the advertising peer voluntarily removes the artifact, a Byzantine peer can exhaust heap memory and crash the replica process.

---

### Finding Description

**Constant definition — the root cause:** [1](#0-0) 

```
/// This limit is used to protect against a malicious peer advertising many ingress messages.
/// If no malicious peers are present the ingress pools are bounded by a separate limit.
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**Channel registration — which artifact types are unprotected:** [2](#0-1) 

Consensus, certifier, DKG, iDKG, and HTTPS-outcalls all pass `SLOT_TABLE_NO_LIMIT`. Only ingress passes `SLOT_TABLE_LIMIT_INGRESS`.

**The guard in `handle_slot_update_receive`:** [3](#0-2) 

```rust
// Only insert slot update if we are below peer slot table limit.
Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => {
    empty_slot.insert(new_slot_entry);
    ...
    (true, None)
}
Entry::Vacant(_) => {
    // slot limit exceeded, drop
    (false, None)
}
```

With `slot_limit = usize::MAX`, the condition `peer_slot_table_len < usize::MAX` is always true in practice (a `usize` counter cannot reach `usize::MAX` before the process OOMs). The guard is a dead branch.

**Task spawning on each new (slot_number, id) pair:** [4](#0-3) 

Every new artifact ID not already in `active_assembles` inserts a `watch::Sender` into the map and spawns a new tokio task.

**Tasks are long-lived — they wait for peer deletion:** [5](#0-4) 

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
```

A task only terminates when the peer counter drops to zero — i.e., when the Byzantine peer removes the artifact from its slot. A malicious peer never does this.

**Topology cleanup does not help:** [6](#0-5) 

`handle_topology_update` only removes peers that leave the subnet topology. A Byzantine peer that remains a subnet member is never cleaned up.

**The design assumption that is violated:** [7](#0-6) 

> "Finite Active Messages Assumption: The design achieves efficiency and simplicity by leveraging the assumption that there is always a finite number of active messages in the system."

A Byzantine peer with `SLOT_TABLE_NO_LIMIT` directly violates this assumption.

---

### Impact Explanation

Each slot update with a fresh `(slot_number, id)` pair permanently occupies:
- One `HashMap` entry in `slot_table[peer_id]`
- One `watch::Sender` entry in `active_assembles`
- One live tokio task in `artifact_processor_tasks` (stack + heap for async state machine + channel state)

Sending N such updates causes O(N) memory growth with no eviction path. At sufficient N the replica process is killed by the OS OOM killer. If enough replicas on a subnet crash simultaneously or in sequence, subnet consensus stalls permanently.

---

### Likelihood Explanation

- **Attacker requirement:** One authenticated subnet node acting Byzantine. This is below the consensus fault threshold (f < n/3) and is the standard Byzantine adversary model.
- **No special privileges needed:** The attacker only needs a valid QUIC/TLS connection to peer replicas, which every subnet node has.
- **Exploit is simple:** Send a tight loop of `SlotUpdate { slot_number: i, commit_id: i, update: Update::Id(unique_id_i) }` for i = 0, 1, 2, … The slot numbers and IDs just need to be distinct.
- **No rate-limiting defense:** There is no application-level rate limit on slot updates per peer beyond the disabled `slot_limit` guard.

---

### Recommendation

Replace `SLOT_TABLE_NO_LIMIT` with a realistic per-artifact-type bound for all artifact channels, not just ingress. The bound should reflect the maximum number of legitimately active artifacts a single honest peer would ever advertise simultaneously (e.g., bounded by consensus round depth, DKG epoch size, etc.). The existing guard logic in `handle_slot_update_receive` is correct — it just needs a non-`usize::MAX` value to be effective.

---

### Proof of Concept

```rust
// Attacker loop (pseudocode for a Byzantine subnet peer):
for i in 0..1_000_000u64 {
    send_slot_update(SlotUpdate {
        slot_number: SlotNumber::from(i),
        commit_id:   CommitId::from(i),
        update:      Update::Id(ConsensusMessageId::unique(i)),
    });
}
// On the honest replica:
// active_assembles.len() == 1_000_000
// artifact_processor_tasks.len() == 1_000_000
// Each task is blocked on peer_rx.wait_for(|p| p.is_empty())
// → OOM
```

To verify locally: construct a `ConsensusManagerReceiver` with `slot_limit = usize::MAX` (the production value), call `handle_slot_update_receive` N times with distinct slot numbers and distinct IDs from a single `peer_id`, and assert `active_assembles.len() == N` — the existing test `slot_table_limit_exceeded` already demonstrates the guard works when given a finite limit, confirming the guard is the only defense.

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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L462-470)
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
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L542-568)
```rust
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

**File:** rs/p2p/consensus_manager/README.adoc (L24-24)
```text
* Finite Active Messages Assumption: The design achieves efficiency and simplicity by leveraging the assumption that there is always a finite number of active messages in the system. This assumption ensures predictable resource usage and prevents attacks that might exploit excessive message production.
```
