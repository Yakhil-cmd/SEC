### Title
Unbounded Slot Table Growth via `SLOT_TABLE_NO_LIMIT` Allows Single Malicious Peer to OOM Subnet Nodes — (`rs/replica/setup_ic_network/src/lib.rs`, `rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

`AbortableBroadcastChannels::new` assigns `SLOT_TABLE_NO_LIMIT = usize::MAX` as the per-peer slot table limit for every artifact type except ingress. `handle_slot_update_receive` enforces this limit only when inserting a new (vacant) slot entry. Because the limit is `usize::MAX`, the vacant-entry guard is never triggered for consensus, certifier, DKG, iDKG, and HTTPS-outcalls artifact types. A single authenticated subnet peer can therefore send an unbounded stream of slot updates with distinct slot numbers and artifact IDs, causing unbounded growth of the `slot_table`, `active_assembles`, and `artifact_processor_tasks` structures on every receiving node, ultimately exhausting memory and crashing the node.

---

### Finding Description

**`AbortableBroadcastChannels::new` — slot limit assignment** [1](#0-0) 

The comment on line 72 explicitly states the ingress limit exists to protect against a malicious peer. No equivalent protection is applied to the other five artifact channels: [2](#0-1) 

All non-ingress channels receive `SLOT_TABLE_NO_LIMIT` (`usize::MAX`).

**`handle_slot_update_receive` — vacant-entry guard** [3](#0-2) 

The guard `peer_slot_table_len < self.slot_limit` is the only check preventing unbounded slot table growth. With `slot_limit = usize::MAX` this condition is always true; the `Entry::Vacant(_)` drop branch (lines 381–390) is unreachable for non-ingress types.

**Unbounded task spawning**

Every new unique artifact ID seen in a slot update spawns a new `process_slot_update` task: [4](#0-3) 

Tasks are only removed from `active_assembles` inside `handle_artifact_processor_joined` when the peer counter becomes empty: [5](#0-4) 

A malicious peer that never overwrites or deletes its advertised slots keeps the peer counter non-zero indefinitely, so tasks accumulate without bound.

**Inbound channel buffer is not a mitigation**

The axum handler feeds a channel of depth 100: [6](#0-5) 

The event loop drains this channel as fast as it can; the channel depth only introduces a brief back-pressure window, not a cap on total entries processed.

---

### Impact Explanation

On every subnet node that receives slot updates from the malicious peer:

- `slot_table[peer_id]` grows without bound (one entry per distinct slot number sent).
- `active_assembles` grows without bound (one entry per distinct artifact ID sent).
- `artifact_processor_tasks` (a `JoinSet`) grows without bound (one Tokio task per distinct artifact ID).

Each task holds a `watch::Receiver<PeerCounter>`, a cloned `Assembler`, a cloned `Sender`, and a `CancellationToken`. At scale this exhausts heap memory, triggering an OOM kill of the replica process. Because all honest nodes are affected simultaneously, the subnet halts.

---

### Likelihood Explanation

The attacker must be an authenticated subnet node (QUIC/TLS uses node certificates). Joining a subnet requires an NNS governance vote, so the bar is higher than a pure external attacker. However, the scope explicitly includes "protocol peer behavior below the consensus fault threshold," and a single compromised or malicious node is sufficient — no threshold majority is needed. Once on the subnet the exploit requires only sending crafted P2P slot-update messages, which is trivially scriptable.

---

### Recommendation

Apply a finite per-peer slot limit to all artifact types, not only ingress. The appropriate bound for each type should reflect the maximum number of distinct artifacts a single honest peer would legitimately advertise per epoch (e.g., bounded by the number of consensus rounds, DKG transcripts, or HTTPS-outcall responses in flight). Alternatively, enforce a global cap on `active_assembles` and `artifact_processor_tasks` regardless of artifact type.

---

### Proof of Concept

```
1. Attacker controls one subnet node N_evil.
2. N_evil opens a QUIC connection to honest node N_victim (authenticated via its node TLS cert).
3. N_evil sends POST /{consensus_uri}/update with a valid protobuf SlotUpdate:
      slot_id   = 1
      commit_id = 1
      update    = Id(<arbitrary_bytes>)
4. N_victim's update_handler decodes the message and forwards it to the event loop.
5. handle_slot_update_receive: peer_slot_table_len=0 < usize::MAX → inserts entry,
   spawns process_slot_update task for artifact ID X.
6. N_evil repeats step 3 with slot_id=2,3,…,N and distinct artifact IDs.
7. After ~10^6 iterations: slot_table[N_evil].len() = 10^6,
   active_assembles.len() = 10^6, artifact_processor_tasks.len() = 10^6.
8. N_victim's heap is exhausted → OOM → replica process killed → node drops out of consensus.
9. Repeating against all honest nodes halts the subnet.
```

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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L43-43)
```rust
    let (update_tx, update_rx) = tokio::sync::mpsc::channel(100);
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L317-319)
```rust
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
