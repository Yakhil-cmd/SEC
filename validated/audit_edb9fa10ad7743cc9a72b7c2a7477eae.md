Audit Report

## Title
Unbounded Per-Peer Slot Table Growth via `SLOT_TABLE_NO_LIMIT` on Non-Ingress P2P Channels Enables OOM Crash — (`rs/replica/setup_ic_network/src/lib.rs`)

## Summary

All non-ingress artifact channels (consensus, certification, DKG, IDKG, HTTPS-outcalls) are wired with `SLOT_TABLE_NO_LIMIT = usize::MAX`, making the per-peer slot table guard in `ConsensusManagerReceiver::handle_slot_update_receive` a no-op. A single Byzantine subnet peer below the BFT fault threshold can exhaust heap memory on any victim replica by advertising an unbounded stream of distinct `SlotUpdate` messages, causing an OOM kill and loss of subnet liveness if enough replicas are crashed.

## Finding Description

In `rs/replica/setup_ic_network/src/lib.rs`, two constants are defined:

```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
``` [1](#0-0) 

The comment at line 72 explicitly acknowledges the ingress limit protects against malicious peers. However, every other channel passes `SLOT_TABLE_NO_LIMIT`:

- Consensus (both code paths): lines 237, 246
- Certification: line 268
- DKG: line 279
- IDKG: line 291
- HTTPS-outcalls: line 303 [2](#0-1) 

Only ingress uses the bounded constant at line 257. [3](#0-2) 

The guard in `rs/p2p/consensus_manager/src/receiver.rs` is:

```rust
Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => { ... }
Entry::Vacant(_) => { /* drop */ }
``` [4](#0-3) 

When `self.slot_limit = usize::MAX`, the condition `peer_slot_table_len < usize::MAX` is always true in practice — memory is exhausted long before `usize::MAX` entries are inserted. Every accepted `(slot_number, id)` pair with a new artifact ID then inserts into `active_assembles` and spawns a new Tokio task in `artifact_processor_tasks`: [5](#0-4) 

Three data structures grow without bound per Byzantine peer: `slot_table[peer_id]`, `active_assembles`, and `artifact_processor_tasks`.

The existing test `slot_table_limit_exceeded` confirms the guard works correctly when a finite limit is set (`slot_limit: 2`), but is never exercised with `usize::MAX` for the non-ingress channels. [6](#0-5) 

## Impact Explanation

A Byzantine peer can crash any victim replica via OOM kill. If `f` replicas are crashed (where `f` is the subnet fault threshold), subnet liveness is permanently lost. This matches the allowed High impact: **"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."** The attack is not volumetric — it exploits a missing per-peer bound in the protocol state machine.

## Likelihood Explanation

The attacker must be a TLS-authenticated subnet node (Byzantine peer below the fault threshold). This is explicitly in-scope for IC's BFT threat model. No cryptographic material needs to be forged. The attack is mechanical: send a tight loop of `SlotUpdate` messages with distinct `slot_number` and `id` values on any unlimited channel. The asymmetry is documented in the source code itself (ingress is protected; others are not), confirming developer awareness of the concern without extension to other channels.

## Recommendation

Apply a per-peer slot table limit to all artifact channels, not only ingress. Define channel-specific constants analogous to `SLOT_TABLE_LIMIT_INGRESS`, sized to the maximum number of in-flight artifacts a legitimate peer would ever advertise simultaneously for each artifact type (bounded by consensus round size, DKG epoch size, etc.). Pass these constants instead of `SLOT_TABLE_NO_LIMIT` in `rs/replica/setup_ic_network/src/lib.rs` for all `abortable_broadcast_channel` calls.

## Proof of Concept

1. Byzantine peer `P` authenticates via TLS to victim replica `V` as a subnet node.
2. `P` sends `N = 10^6` `SlotUpdate` messages on the certification (or DKG/IDKG/HTTPS-outcalls/consensus) channel, each with a distinct `slot_number: i`, `commit_id: i`, `update: Id(unique_id_i)`.
3. `ConsensusManagerReceiver::handle_slot_update_receive` processes each: `slot_table[P]` grows to `N` entries (limit check never fires), `active_assembles` grows to `N` entries, `artifact_processor_tasks` spawns `N` Tokio tasks.
4. Replica `V`'s RSS grows proportionally to `N` until OOM kill.
5. Differential: repeat with the ingress channel — capped at 50,000 entries, no OOM.
6. Unit test path: extend `slot_table_limit_exceeded` in `rs/p2p/consensus_manager/src/receiver.rs` with `slot_limit: usize::MAX` and a large `N`; observe unbounded growth vs. the bounded ingress case.

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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L847-887)
```rust
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
