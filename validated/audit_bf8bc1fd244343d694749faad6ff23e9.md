Audit Report

## Title
Unbounded Per-Peer Slot Table Growth via `SLOT_TABLE_NO_LIMIT = usize::MAX` Enables OOM Crash on Honest Replicas — (`rs/replica/setup_ic_network/src/lib.rs` / `rs/p2p/consensus_manager/src/receiver.rs`)

## Summary

`SLOT_TABLE_NO_LIMIT = usize::MAX` is assigned as the per-peer slot table cap for consensus, certifier, DKG, iDKG, and HTTPS-outcalls artifact channels. The guard in `handle_slot_update_receive` that enforces this cap is structurally correct but rendered a dead branch because `peer_slot_table_len < usize::MAX` is always true in practice. A single authenticated Byzantine subnet peer can therefore insert an unbounded number of `(slot_number, artifact_id)` pairs, each permanently occupying a `HashMap` entry, a `watch::Sender`, and a live tokio task that blocks indefinitely waiting for the Byzantine peer to voluntarily remove the artifact — which it never does. This causes unbounded O(N) heap growth and eventual OOM crash of the replica process.

## Finding Description

**Root cause — constant definition:** [1](#0-0) 

`SLOT_TABLE_NO_LIMIT = usize::MAX` is the slot limit passed to `abortable_broadcast_channel` for every artifact type except ingress.

**Unprotected artifact channels:** [2](#0-1) 

Consensus (both paths), certifier, DKG, iDKG, and HTTPS-outcalls all pass `SLOT_TABLE_NO_LIMIT`. Only ingress passes `SLOT_TABLE_LIMIT_INGRESS = 50_000`.

**The guard is a dead branch:** [3](#0-2) 

The condition `peer_slot_table_len < self.slot_limit` with `slot_limit = usize::MAX` is always satisfied; the `Entry::Vacant(_)` drop branch at line 381 is unreachable in practice.

**Task spawned per new artifact ID:** [4](#0-3) 

Every new `(slot_number, id)` pair inserts a `watch::Sender` into `active_assembles` and spawns a new tokio task via `artifact_processor_tasks.spawn_on(...)`.

**Tasks block indefinitely for Byzantine peers:** [5](#0-4) [6](#0-5) 

Whether the artifact assembles successfully (`AssembleResult::Done`) or is unwanted (`AssembleResult::Unwanted`), the task reaches `peer_rx.wait_for(|p| p.is_empty()).await`. A Byzantine peer that never sends a removal update keeps the `PeerCounter` non-empty forever, so the task never exits.

**Topology cleanup does not help:** [7](#0-6) 

`handle_topology_update` only removes peers that leave the subnet topology. A Byzantine peer that remains a subnet member is never evicted.

**Exploit path:** Byzantine peer sends a tight loop of `SlotUpdate { slot_number: i, commit_id: i, update: Update::Id(unique_id_i) }` for `i = 0, 1, 2, …`. Each iteration inserts one `HashMap` entry in `slot_table[peer_id]`, one entry in `active_assembles`, and spawns one tokio task. All tasks remain alive indefinitely. Memory grows without bound until the OS OOM-kills the replica process.

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."* A single Byzantine subnet peer can crash one or more honest replicas via OOM. If enough replicas on a subnet are crashed in sequence (each crash is permanent until the process is restarted and state is re-synced), subnet consensus can stall, causing certified-state disruption and subnet unavailability.

## Likelihood Explanation

- **Attacker requirement:** One authenticated subnet node acting Byzantine — below the consensus fault threshold (f < n/3) and the standard Byzantine adversary model.
- **No special privileges:** The attacker only needs a valid QUIC/TLS connection to peer replicas, which every subnet node possesses.
- **Exploit is simple and cheap:** Sending distinct slot updates in a tight loop requires no cryptographic work beyond the existing peer TLS identity.
- **No application-level rate limit:** The only defense — the `slot_limit` guard — is disabled by `usize::MAX` for all affected artifact types.
- **Repeatable:** The attack can be re-launched after a replica restarts, preventing recovery.

## Recommendation

Replace `SLOT_TABLE_NO_LIMIT` with a realistic per-artifact-type bound for all artifact channels, not just ingress. The bound should reflect the maximum number of legitimately active artifacts a single honest peer would ever advertise simultaneously (e.g., bounded by consensus round depth, DKG epoch size, number of active iDKG transcripts, etc.). The existing guard logic in `handle_slot_update_receive` is structurally correct — it only needs a finite, non-`usize::MAX` value to be effective.

## Proof of Concept

Construct a `ConsensusManagerReceiver` with `slot_limit = usize::MAX` (the production value for consensus/certifier/DKG/iDKG/HTTPS-outcalls). Call `handle_slot_update_receive` N times from a single `peer_id`, each time with a distinct `slot_number` and a distinct artifact `id`. Assert:

```
active_assembles.len() == N
artifact_processor_tasks.len() == N  // all tasks alive, blocked on peer_rx.wait_for(...)
```

The existing test `slot_table_limit_exceeded` in `receiver.rs` already demonstrates the guard works correctly when given a finite limit — confirming the guard is the sole defense and that setting `usize::MAX` disables it entirely.

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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L498-522)
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
