### Title
Unbounded Slot Table Growth via `SLOT_TABLE_NO_LIMIT` Enables Single-Peer Resource Exhaustion DoS — (`rs/replica/setup_ic_network/src/lib.rs`, `rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

`AbortableBroadcastChannels::new` configures five of six P2P artifact channels with `SLOT_TABLE_NO_LIMIT = usize::MAX`. A single authenticated subnet peer can exploit this to grow the receiver's slot table, `active_assembles` map, and `artifact_processor_tasks` JoinSet without bound, causing OOM or CPU exhaustion on every honest replica it connects to.

---

### Finding Description

In `rs/replica/setup_ic_network/src/lib.rs`, two slot-limit constants are defined:

```
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;   // only ingress is bounded
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;     // consensus, certifier, dkg, idkg, https_outcalls
``` [1](#0-0) 

The comment on line 72 explicitly acknowledges the threat model ("protect against a malicious peer advertising many ingress messages") but the same protection is absent for the other five artifact types. [2](#0-1) 

In `handle_slot_update_receive`, the guard that enforces the limit is:

```rust
Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => { … (true, None) }
Entry::Vacant(_) => { /* drop */ (false, None) }
``` [3](#0-2) 

With `slot_limit = usize::MAX`, the condition `peer_slot_table_len < usize::MAX` is always `true` — the drop branch is unreachable. Every new `(peer_id, slot_number)` pair is unconditionally inserted.

When `to_add` is `true` and the artifact ID has not been seen before, the code immediately spawns a new Tokio task and inserts into `active_assembles`:

```rust
self.active_assembles.insert(id.clone(), tx);
self.artifact_processor_tasks.spawn_on(
    Self::process_slot_update(…),
    &self.rt_handle,
);
``` [4](#0-3) 

Each spawned `process_slot_update` task blocks waiting for either the peer to retract the slot or the artifact to be assembled. A malicious peer that never retracts and never delivers the artifact keeps every task alive indefinitely.

---

### Impact Explanation

Three in-memory structures grow without bound per malicious peer:

| Structure | Growth trigger |
|---|---|
| `slot_table[peer_id]` | one entry per unique `slot_number` |
| `active_assembles` | one entry per unique artifact `id` |
| `artifact_processor_tasks` | one live Tokio task per unique artifact `id` |

Sending N distinct slot numbers with N distinct artifact IDs allocates O(N) HashMap entries and spawns O(N) Tokio tasks. At scale this causes OOM on the victim replica, crashing the process and halting its participation in consensus. Because the attack is per-connection, a single malicious node can target every honest peer it is connected to simultaneously.

---

### Likelihood Explanation

The attacker must be an authenticated subnet node (QUIC/TLS mutual auth). This is within the explicit scope of "protocol peer behavior below the consensus fault threshold." A single compromised or malicious node operator can execute this without any governance majority. The attack is trivially scriptable: send a tight loop of `SlotUpdate { slot_number: i, commit_id: i, update: Update::Id(random_id) }` messages over the existing P2P connection.

---

### Recommendation

Apply a finite per-peer slot limit to all artifact channels, not just ingress. The existing `SLOT_TABLE_LIMIT_INGRESS = 50_000` pattern is the correct model. Appropriate limits for each artifact type should be derived from the maximum number of in-flight artifacts that the protocol can legitimately produce per round (e.g., consensus has a bounded number of blocks/notarizations per height). Remove or rename `SLOT_TABLE_NO_LIMIT` to prevent future misuse.

---

### Proof of Concept

```
// Attacker is a legitimate subnet node.
// For each victim replica R connected to attacker:
for i in 0..usize::MAX {
    send_slot_update(R, artifact_type=Consensus, SlotUpdate {
        slot_number: SlotNumber(i),
        commit_id:   CommitId(i),
        update:      Update::Id(random_unique_id()),
    });
    // Never send the artifact body; never retract the slot.
}
// R's slot_table[attacker], active_assembles, and artifact_processor_tasks
// grow by one entry per iteration until OOM kills the replica process.
```

The slot limit guard at line 373 of `receiver.rs` never fires because `peer_slot_table_len < usize::MAX` is always `true`. [5](#0-4)

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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L399-419)
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
```
