After carefully reading the code, here is my analysis:

## Code Trace

**Slot limit enforcement** (`receiver.rs:373-390`): A per-peer slot table is capped at `slot_limit` entries. Updates beyond the limit are dropped with a warning. So a single peer can have at most `slot_limit` entries.

**Task spawning** (`receiver.rs:393-421`): For each new slot entry whose artifact ID is not already in `active_assembles`, a new `artifact_processor_tasks` task is spawned. N distinct IDs → N tasks.

**The blocking point** (`receiver.rs:480-536`): `process_slot_update` runs a `select!` with three branches:
1. `cancellation_token.cancelled()` — shutdown
2. `assemble_artifact` — assembly completes
3. `all_peers_deleted_artifact` — peer counter empties *before* assembly

If branch 2 fires first (assembly completes), the code falls through to a bare `await` at lines 500 and 521:
```rust
// TODO: NET-1774
let _ = peer_rx.wait_for(|p| p.is_empty()).await;
```
This `await` is **outside** the `select!` — the cancellation token is no longer checked. The task is stuck until the peer counter empties.

**When does the peer counter empty?**
- Peer overwrites the slot (Byzantine peer won't)
- Peer leaves the topology (`handle_topology_update`, lines 547-563)
- `watch::Sender` is dropped (only on system shutdown, since the sender lives in `active_assembles` which is only cleaned up in `handle_artifact_processor_joined`, which is only called when the task *finishes* — a circular dependency)

**Production slot limits** (`rs/replica/setup_ic_network/src/lib.rs:74-75`):
```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```
Ingress uses `50_000`. Consensus, certifier, DKG, iDKG, and HTTPS outcalls all use `usize::MAX` — **no limit at all**.

**The TODO confirms awareness**: The `// TODO: NET-1774` comment appears at both `wait_for` call sites (lines 499 and 519), explicitly flagging this as a known unresolved issue.

---

### Title
Byzantine Peer Can Exhaust Replica Task Resources via Indefinitely Blocked `artifact_processor_tasks` — (`rs/p2p/consensus_manager/src/receiver.rs`)

### Summary
A single Byzantine peer below the fault threshold can cause O(N) `artifact_processor_tasks` to block indefinitely at `peer_rx.wait_for(|p| p.is_empty())`, consuming unbounded memory and Tokio task resources, with no cancellation escape until the peer leaves the subnet topology.

### Finding Description

When a Byzantine peer sends slot updates with N distinct artifact IDs (and optionally full artifact payloads), N tasks are spawned in `artifact_processor_tasks`. [1](#0-0) 

Each task runs `process_slot_update`. If the `assemble_artifact` branch of the outer `select!` completes first (e.g., because the peer pushed the full artifact, or the assembler returned `Unwanted`), the task exits the `select!` and enters a bare `await`: [2](#0-1) 

At this point, the cancellation token is no longer polled. The task is stuck until `peer_rx` reports an empty peer counter. The peer counter for this artifact ID is only decremented when:
- The peer overwrites the slot (Byzantine peer refuses to do this), or
- The peer leaves the subnet topology. [3](#0-2) 

A Byzantine peer that remains a valid subnet member never triggers the topology escape. The `watch::Sender` stored in `active_assembles` is only dropped when `handle_artifact_processor_joined` removes it — but that function is only called when the task finishes, creating a deadlock. [4](#0-3) 

### Impact Explanation

For **ingress**, the slot limit is 50,000 per peer. [5](#0-4) 

With f Byzantine peers (e.g., f=4 on a 13-node subnet), up to 200,000 tasks can be permanently blocked, each holding a `watch` channel, artifact data, and Tokio task overhead.

For **consensus, certifier, DKG, iDKG, and HTTPS outcalls**, the slot limit is `usize::MAX` — effectively unbounded. [6](#0-5) 

This can lead to memory exhaustion and Tokio runtime task-pool saturation, stalling the consensus event loop.

### Likelihood Explanation

The attacker is a Byzantine replica peer — a valid attacker model for IC's BFT threat model. The attack requires only sending crafted P2P slot update messages with distinct artifact IDs and valid artifact payloads. No privileged access, key material, or majority corruption is needed. The developers themselves have flagged this with `// TODO: NET-1774` at both blocking sites. [7](#0-6) [8](#0-7) 

### Recommendation

Replace the bare `peer_rx.wait_for(|p| p.is_empty()).await` calls with a `select!` that also polls the cancellation token, so tasks can be aborted on shutdown and on explicit abort signals. Additionally, enforce a meaningful slot limit for all artifact types (not just ingress), and consider adding a per-task timeout so that tasks advertising artifacts that are never retracted are eventually reaped.

### Proof of Concept

State-machine test (no network needed):
1. Construct a `ConsensusManagerReceiver` with `slot_limit = 10` and a mock assembler that immediately returns `AssembleResult::Done`.
2. Inject 10 `SlotUpdate` messages from one peer, each with a distinct artifact ID and a full artifact payload.
3. Assert `artifact_processor_tasks.len() == 10` and `active_assembles.len() == 10`.
4. Wait for all 10 tasks to reach the `wait_for` barrier (observable via metrics or a condvar in the mock).
5. Assert that after an arbitrary timeout, `artifact_processor_tasks.len()` is still 10 — tasks have not terminated.
6. Trigger a topology update removing the peer; assert all tasks terminate promptly.

### Citations

**File:** rs/p2p/consensus_manager/src/receiver.rs (L290-319)
```rust
    pub(crate) fn handle_artifact_processor_joined(
        &mut self,
        peer_rx: watch::Receiver<PeerCounter>,
        id: WireArtifact::Id,
        cancellation_token: CancellationToken,
    ) {
        self.metrics.assemble_task_finished_total.inc();
        // Invariant: Peer sender should only be dropped in this task..
        debug_assert!(peer_rx.has_changed().is_ok());

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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L407-419)
```rust
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L498-521)
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
```

**File:** rs/p2p/consensus_manager/src/receiver.rs (L542-563)
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
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L74-75)
```rust
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
