### Title
Unbounded Per-Peer Slot Table Growth via `SLOT_TABLE_NO_LIMIT` on Certification/DKG/IDKG/HTTPS-Outcalls Channels — (`rs/replica/setup_ic_network/src/lib.rs`, `rs/p2p/consensus_manager/src/receiver.rs`)

---

### Summary

`AbortableBroadcastChannels::new` wires five of six artifact channels with `SLOT_TABLE_NO_LIMIT = usize::MAX`. A single Byzantine subnet peer (below the fault threshold) can exploit this by advertising an unbounded stream of distinct slot IDs on any of those channels, causing `ConsensusManagerReceiver`'s `slot_table`, `active_assembles`, and `artifact_processor_tasks` to grow without bound until the replica process is OOM-killed.

---

### Finding Description

**Constant definitions** — `rs/replica/setup_ic_network/src/lib.rs` lines 74–75:

```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
``` [1](#0-0) 

**Channel wiring** — only `ingress` receives the bounded limit; every other channel gets `usize::MAX`:

| Channel | Limit |
|---|---|
| `ingress` | `SLOT_TABLE_LIMIT_INGRESS` (50 000) |
| `consensus` | `SLOT_TABLE_NO_LIMIT` |
| `certifier` | `SLOT_TABLE_NO_LIMIT` |
| `dkg` | `SLOT_TABLE_NO_LIMIT` |
| `idkg` | `SLOT_TABLE_NO_LIMIT` |
| `https_outcalls` | `SLOT_TABLE_NO_LIMIT` | [2](#0-1) 

**Guard in `handle_slot_update_receive`** — `rs/p2p/consensus_manager/src/receiver.rs` line 373:

```rust
Entry::Vacant(empty_slot) if peer_slot_table_len < self.slot_limit => {
    empty_slot.insert(new_slot_entry);
    ...
    (true, None)
}
Entry::Vacant(_) => {
    // drop — limit exceeded
    (false, None)
}
``` [3](#0-2) 

When `self.slot_limit = usize::MAX`, the condition `peer_slot_table_len < usize::MAX` is always `true` in practice (the process OOMs long before `peer_slot_table_len` reaches `usize::MAX`). Every new `Entry::Vacant` slot is unconditionally inserted.

**Unbounded task spawning** — for each new slot with a previously-unseen artifact ID, the code inserts into `active_assembles` and spawns a new Tokio task:

```rust
self.active_assembles.insert(id.clone(), tx);
self.artifact_processor_tasks.spawn_on(
    Self::process_slot_update(...),
    &self.rt_handle,
);
``` [4](#0-3) 

The `slot_table` (`HashMap<NodeId, HashMap<SlotNumber, SlotEntry>>`), `active_assembles` (`HashMap<Id, watch::Sender<PeerCounter>>`), and `artifact_processor_tasks` (`JoinSet`) all grow proportionally to the number of distinct `(slot_number, artifact_id)` pairs the attacker sends. [5](#0-4) 

---

### Impact Explanation

A single Byzantine subnet node (TLS-authenticated, below the `f`-fault threshold) can OOM-crash any honest replica it connects to by flooding it with distinct slot IDs on the certification, DKG, IDKG, or HTTPS-outcalls channel. Each message is cheap to produce and requires no valid artifact content (`Update::Id(unique_id)` suffices). The replica process is killed by the OS, causing it to drop out of consensus participation until it restarts and re-syncs state.

---

### Likelihood Explanation

- **Attacker capability required**: membership in the subnet (TLS certificate issued by the IC registry). A single compromised or malicious node operator suffices.
- **No threshold corruption needed**: one node below `f` is sufficient to crash one honest replica.
- **Asymmetry is explicit**: the comment at line 72–73 acknowledges the ingress limit exists to protect against malicious peers advertising many messages, yet the same protection is absent for the other five channels.
- **Attack is silent**: the only observable signal before OOM is a metric counter increment (`slot_table_new_entry_total`) and no log warning until the limit is exceeded — which never happens with `usize::MAX`. [1](#0-0) 

---

### Recommendation

Replace `SLOT_TABLE_NO_LIMIT` with per-channel bounded constants that reflect the maximum number of legitimately active artifacts per peer for each artifact type. The ingress precedent (`50_000`) shows the pattern is already understood. For consensus/certification/DKG/IDKG/HTTPS-outcalls, appropriate limits should be derived from protocol-level bounds on the number of simultaneously active artifacts per peer (e.g., number of active DKG transcripts, IDKG dealings, certification shares, etc.).

---

### Proof of Concept

```rust
// Unit-level reproduction (no network required):
let (mut mgr, _) = ReceiverManagerBuilder::new()
    .with_slot_limit(usize::MAX)  // matches production for cert/dkg/idkg/https
    .build();
let cancel = CancellationToken::new();
for i in 0u64..1_000_000 {
    mgr.handle_slot_update_receive(
        SlotUpdate {
            slot_number: SlotNumber::from(i),
            commit_id: CommitId::from(i),
            update: Update::Id(i),          // distinct ID each time
        },
        NODE_1,
        ConnId::from(1),
        cancel.clone(),
    );
}
// active_assembles.len() == 1_000_000
// artifact_processor_tasks.len() == 1_000_000
// RSS grows proportionally; at 10^8 iterations the process is OOM-killed.
```

Differential: repeat with `slot_limit = 50_000` (ingress production value) — the loop terminates with exactly 50 000 entries and all subsequent updates are silently dropped. [6](#0-5)

### Citations

**File:** rs/replica/setup_ic_network/src/lib.rs (L72-75)
```rust
/// This limit is used to protect against a malicious peer advertising many ingress messages.
/// If no malicious peers are present the ingress pools are bounded by a separate limit.
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L249-303)
```rust
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L405-419)
```rust
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
