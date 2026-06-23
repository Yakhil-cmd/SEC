I have traced the full call chain and examined all relevant guards. Here is my assessment:

---

### Title
Unbounded Stalled Tokio Task Accumulation via `MaybeWantsLater` + `SLOT_TABLE_NO_LIMIT` — (`rs/p2p/consensus_manager/src/receiver.rs`, `rs/p2p/artifact_downloader/src/fetch_artifact/download.rs`)

### Summary

A Byzantine subnet peer can spawn an unbounded number of stalled tokio tasks on an honest replica by sending slot updates with distinct artifact IDs at heights that cause `compute_bouncer` to return `MaybeWantsLater`. For consensus, DKG, IDKG, certifier, and HTTPS-outcalls artifact types, the per-peer slot limit is set to `usize::MAX`, providing no effective bound on task creation.

### Finding Description

**Step 1 — No per-peer slot limit for consensus artifacts.**

In `rs/replica/setup_ic_network/src/lib.rs`:

```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
``` [1](#0-0) 

Consensus, certifier, DKG, IDKG, and HTTPS-outcalls all pass `SLOT_TABLE_NO_LIMIT`: [2](#0-1) 

Only ingress uses a finite limit of 50,000.

**Step 2 — Per-peer slot table guard is bypassed when `slot_limit = usize::MAX`.**

In `handle_slot_update_receive`, new slot entries are rejected only when `peer_slot_table_len >= self.slot_limit`: [3](#0-2) 

With `slot_limit = usize::MAX`, the `Entry::Vacant(_)` drop branch is unreachable. Every new distinct `(peer_id, slot_number)` pair is accepted.

**Step 3 — Each new unique artifact ID spawns a new tokio task.**

When a slot update arrives with an ID not already in `active_assembles`, a new `process_slot_update` task is spawned unconditionally: [4](#0-3) 

**Step 4 — Each task blocks indefinitely on `bouncer_watcher.changed()` for far-future IDs.**

`should_download` loops on the watch channel while the bouncer returns `MaybeWantsLater`: [5](#0-4) 

The consensus bouncer returns `MaybeWantsLater` for any non-CUP artifact at height `> next_cup_height + ACCEPTABLE_NOTARIZATION_CUP_GAP`: [6](#0-5) 

The bouncer refreshes every 3 seconds, but far-future IDs will always re-evaluate to `MaybeWantsLater`, so the loop never exits.

**Step 5 — Task termination only occurs on peer disconnect or slot overwrite.**

`process_slot_update` selects on `all_peers_deleted_artifact` (fires when `PeerCounter` empties) or `cancellation_token`. As long as the Byzantine peer holds the slot open and stays connected, the task lives: [7](#0-6) 

### Impact Explanation

A single Byzantine subnet peer can:
1. Send N slot updates on N distinct slot numbers, each with a distinct far-future consensus artifact ID.
2. Cause N `process_slot_update` tasks to be spawned, each blocked in `should_download` on `bouncer_watcher.changed()`.
3. Each task holds a `watch::Receiver<Bouncer<Id>>`, a `watch::Receiver<PeerCounter>`, and async stack frames.
4. With `slot_limit = usize::MAX`, N is unbounded while the peer remains connected.

With `f` Byzantine peers each sending at their network-rate maximum, the honest replica accumulates O(f × rate × time) stalled tasks, leading to heap exhaustion or tokio thread-pool starvation, degrading availability.

### Likelihood Explanation

- Attacker must be an authenticated subnet member (Byzantine node, f < threshold). This is a realistic threat model for IC subnets.
- No cryptographic material needs to be compromised; the attacker simply sends well-formed but adversarial slot updates over its legitimate QUIC connection.
- The rate is bounded by network bandwidth, but there is no code-level cap for consensus artifacts.
- The path is concrete and locally testable: spawn a mock peer, send N slot updates with distinct far-future IDs, observe task count growth.

### Recommendation

1. Apply a finite per-peer slot limit for all artifact types, not just ingress. The comment at line 72–74 of `setup_ic_network/src/lib.rs` acknowledges the ingress limit exists "to protect against a malicious peer advertising many ingress messages" — the same rationale applies to consensus, DKG, IDKG, certifier, and HTTPS-outcalls.
2. In `should_download`, add a timeout or a maximum wait count so that tasks stalled on `MaybeWantsLater` for longer than a configurable duration self-terminate rather than waiting indefinitely.
3. Consider adding a global cap on `artifact_processor_tasks.len()` in addition to the per-peer cap.

### Proof of Concept

```
1. Connect a mock Byzantine peer to a test replica via QUIC.
2. Send slot updates: for i in 0..100_000:
     SlotUpdate { slot_number: i, commit_id: 1, update: Id(far_future_consensus_id(i)) }
3. Observe active_assembles.len() and artifact_processor_tasks.len() grow to 100,000.
4. Measure heap allocation and tokio task count after 60 s.
5. Confirm tasks remain alive (bouncer keeps returning MaybeWantsLater for far-future heights).
6. Confirm tasks terminate only after the peer disconnects (topology update).
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L480-536)
```rust
        select! {
            _ = cancellation_token.cancelled() => {}
            assemble_result = assemble_artifact => {
                match assemble_result {
                    AssembleResult::Done { message, peer_id } => {
                        let id = message.id();
                        // Sends artifact to the pool. In theory this channel can get full if there is a bug in consensus
                        // and each round takes very long time. However, the duration of this await is not IO-bound so for
                        // the time being it is fine that sending over the channel is not done as part of a select.
                        if sender.send(UnvalidatedArtifactMutation::Insert((message, peer_id))).await.is_err() {
                            error!(
                                log,
                                "The receiving side of the channel, owned by the consensus thread, was closed. \
                                This should be an infallible situation since a cancellation token should be received. \
                                If this happens then most likely there is a very serious synchonization bug."
                            );
                        }

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
                            .assemble_task_result_total
                            .with_label_values(&[ASSEMBLE_TASK_RESULT_DROP])
                            .inc();

                    },
                }
            }
            _ = all_peers_deleted_artifact => {
                metrics
                    .assemble_task_result_total
                    .with_label_values(&[ASSEMBLE_TASK_RESULT_ALL_PEERS_DELETED])
                    .inc();
            },
        };
```

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L186-191)
```rust
        while let BouncerValue::MaybeWantsLater = bouncer_value {
            let _ = bouncer_watcher.changed().await;
            bouncer_value = bouncer_watcher.borrow_and_update()(id);
        }

        BouncerValue::Unwanted != bouncer_value
```

**File:** rs/consensus/src/consensus/priority.rs (L54-60)
```rust
    // Stash non-CUP artifacts, as long as they're too far ahead of the next pending CUP height.
    // This prevents nodes that have fallen behind from exceeding their validated pool bounds.
    if !matches!(id.hash, ConsensusMessageHash::CatchUpPackage(_))
        && height > next_cup_height + Height::new(ACCEPTABLE_NOTARIZATION_CUP_GAP)
    {
        return MaybeWantsLater;
    }
```
