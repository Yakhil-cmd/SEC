### Title
Unbounded Memory Exhaustion via `SLOT_TABLE_NO_LIMIT` in Non-Ingress P2P Channels — (`rs/replica/setup_ic_network/src/lib.rs`, `rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

`AbortableBroadcastChannels::new` configures five of six artifact broadcast channels with `SLOT_TABLE_NO_LIMIT = usize::MAX`. A single authenticated subnet peer (below the consensus fault threshold) can send an unbounded stream of slot updates with distinct slot numbers and artifact IDs, causing the per-peer slot table, the global `active_assembles` map, and the `artifact_processor_tasks` JoinSet to grow without bound, leading to OOM crash or severe performance degradation on honest replica nodes.

---

### Finding Description

In `AbortableBroadcastChannels::new`, six artifact channels are created. Only the ingress channel is given a finite slot limit: [1](#0-0) 

```
/// This limit is used to protect against a malicious peer advertising many ingress messages.
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

The remaining five channels — consensus, certifier, dkg, idkg, and https_outcalls — are all passed `SLOT_TABLE_NO_LIMIT`: [2](#0-1) 

In `handle_slot_update_receive`, the slot limit is enforced only for new (vacant) slot entries: [3](#0-2) 

When a new slot entry is accepted and the artifact ID has not been seen before, a new Tokio task is unconditionally spawned and a new entry is inserted into `active_assembles`: [4](#0-3) 

The `slot_table`, `active_assembles`, and `artifact_processor_tasks` data structures are all unbounded for the five non-ingress channels: [5](#0-4) 

---

### Impact Explanation

A single malicious subnet peer sends slot updates with monotonically increasing `slot_number` values (1, 2, 3, … N) and distinct artifact IDs on any of the five `SLOT_TABLE_NO_LIMIT` channels. For each message:

- A new row is inserted into `slot_table[peer_id]` (unbounded HashMap growth).
- A new entry is inserted into `active_assembles` (unbounded HashMap growth).
- A new async task is spawned into `artifact_processor_tasks` (unbounded JoinSet growth).

Each spawned `process_slot_update` task holds a `watch::Receiver<PeerCounter>`, a cloned `Assembler`, and a `CancellationToken`, and blocks waiting for artifact assembly or peer-counter drain. A malicious peer can produce new slot updates faster than tasks complete, causing monotonically increasing heap and task-stack consumption. The result is OOM crash or replica stall on every honest node that receives the peer's messages, which can halt subnet consensus.

---

### Likelihood Explanation

The attack requires control of exactly one subnet node — below the BFT fault threshold. The QUIC transport authenticates peers via TLS, so only subnet members can send slot updates; however, a single compromised or malicious node is explicitly within the IC threat model ("protocol peer behavior below the consensus fault threshold"). The attack is trivially scriptable: send a tight loop of protobuf-encoded `SlotUpdate` messages with incrementing `slot_id` and unique `id` fields over the existing authenticated connection. No governance action, key compromise, or majority collusion is required.

---

### Recommendation

Apply a finite per-peer slot limit to all five non-ingress channels, analogous to `SLOT_TABLE_LIMIT_INGRESS`. The appropriate bound for each artifact type should reflect the maximum number of in-flight artifacts a legitimate peer would ever advertise simultaneously (e.g., a small multiple of the expected consensus round depth for consensus messages, the committee size for certifications, etc.). Additionally, consider bounding `active_assembles` independently of the slot table, since the same artifact ID can be re-inserted after a task completes.

---

### Proof of Concept

```
// Pseudocode: malicious subnet node sends to honest peer
for slot_id in 0u64.. {
    let msg = pb::SlotUpdate {
        commit_id: slot_id,
        slot_id,
        update: Some(pb::slot_update::Update::Id(
            unique_artifact_id(slot_id).encode_to_vec()
        )),
    };
    transport.send(honest_peer, "/consensus/update", msg.encode_to_vec()).await;
    // Each iteration: +1 slot_table entry, +1 active_assembles entry, +1 spawned task
}
// honest peer's heap grows without bound → OOM
```

The five vulnerable channels are consensus, certifier, dkg, idkg, and https_outcalls — all configured with `SLOT_TABLE_NO_LIMIT = usize::MAX` at: [2](#0-1)

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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L187-195)
```rust
    slot_table: HashMap<NodeId, HashMap<SlotNumber, SlotEntry<WireArtifact::Id>>>,
    active_assembles: HashMap<WireArtifact::Id, watch::Sender<PeerCounter>>,

    #[allow(clippy::type_complexity)]
    artifact_processor_tasks: JoinSet<(watch::Receiver<PeerCounter>, WireArtifact::Id)>,

    topology_watcher: watch::Receiver<SubnetTopology>,

    slot_limit: usize,
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
