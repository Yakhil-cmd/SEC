Based on my analysis of the actual code, here is my assessment:

---

### Title
O(N²) CPU Exhaustion via `Vec::contains` Linear Scan in `HeightIndex::insert` with Unbounded Consensus Slot Table — (`rs/artifact_pool/src/height_index.rs`)

### Summary

The `HeightIndex::insert` function uses `Vec::contains` for deduplication, which is O(N) per call. A Byzantine P2P peer can exploit the fact that consensus artifacts have **no per-peer slot table limit** (`SLOT_TABLE_NO_LIMIT`) to inject N cryptographically distinct artifact hashes at the same consensus height, causing O(N²) total CPU work in the consensus thread's unvalidated pool insertion path.

### Finding Description

**Root cause — O(N) deduplication in `HeightIndex::insert`:** [1](#0-0) 

Each call to `insert` at a height with an existing bucket of size K performs a full `Vec::contains` scan of K elements. For N distinct values inserted at the same height, total work is 0+1+2+…+(N-1) = **O(N²)**.

**The unvalidated consensus pool uses this index directly:** [2](#0-1) 

`self.indexes.insert(msg, &hash)` is called unconditionally before the O(log N) BTreeMap deduplication check, so every insertion — including duplicates — triggers the linear scan.

**The P2P layer enforces no slot limit for consensus artifacts:** [3](#0-2) 

Both consensus code paths pass `SLOT_TABLE_NO_LIMIT`. Contrast this with ingress, which uses `SLOT_TABLE_LIMIT_INGRESS`. The per-peer slot table guard in the receiver: [4](#0-3) 

…is bypassed for consensus because `slot_limit = SLOT_TABLE_NO_LIMIT` (effectively `usize::MAX`), so the `Entry::Vacant(_)` drop branch is never reached.

**Attack path:**

1. Byzantine peer sends N `SlotUpdate` messages, each on a distinct slot number, each carrying a distinct `NotarizationShare` artifact (different content → different `CryptoHashOf<NotarizationShare>`) all at height H. Artifacts are ≤1 KB so they are pushed inline (below `ARTIFACT_PUSH_THRESHOLD_BYTES = 1024`).
2. The `ConsensusManagerReceiver` spawns N `process_slot_update` tasks. The bouncer accepts shares at the current height.
3. Each assembled artifact is sent via channel to the consensus thread as `UnvalidatedArtifactMutation::Insert`.
4. The consensus thread calls `MutablePool::insert` → `InMemoryPoolSection::insert` → `Indexes::insert` → `HeightIndex::insert` for the `notarization_share` bucket.
5. The k-th insertion scans k−1 existing entries. For N=10,000 insertions: ~50 million hash comparisons (32 bytes each) ≈ **~1.6 billion byte comparisons** executed synchronously on the consensus thread. [5](#0-4) 

The `notarization_share` index is exactly the type targeted.

### Impact Explanation

The consensus thread is stalled processing O(N²) `Vec::contains` work. This delays or halts the consensus processing loop on the targeted replica, causing it to fall behind the subnet. A single Byzantine peer below the fault threshold (f < n/3) can trigger this without exceeding any per-peer byte quota, since the slot table is unbounded for consensus artifacts and each artifact is small.

### Likelihood Explanation

The attack requires only a single Byzantine subnet peer (a node that has been compromised or is acting maliciously). The exploit is deterministic and locally reproducible. No threshold corruption, no volumetric traffic, no crypto break required. The attacker controls the artifact content and can generate arbitrarily many distinct hashes.

### Recommendation

Replace `Vec<T>` with `HashSet<T>` (requiring `T: Hash`) or `BTreeSet<T>` (requiring `T: Ord`) in `HeightIndex` buckets to reduce per-insertion deduplication from O(N) to O(1) amortized or O(log N). `CryptoHashOf<T>` already implements `Hash` and `Ord`, so this is a straightforward substitution. Alternatively, enforce a finite `slot_limit` for consensus artifacts analogous to `SLOT_TABLE_LIMIT_INGRESS`.

### Proof of Concept

```rust
// Criterion benchmark: insert 10_000 distinct CryptoHashOf<NotarizationShare> at height=1
let mut index: HeightIndex<CryptoHashOf<NotarizationShare>> = HeightIndex::new();
let h = Height::from(1);
for i in 0u64..10_000 {
    let hash = CryptoHashOf::from(CryptoHash(i.to_le_bytes().to_vec()));
    index.insert(h, &hash);  // k-th call scans k-1 elements
}
// Wall-clock time grows quadratically; a HashMap-backed impl is flat O(1) amortized.
``` [6](#0-5) [7](#0-6)

### Citations

**File:** rs/artifact_pool/src/height_index.rs (L9-11)
```rust
pub struct HeightIndex<T: Eq> {
    buckets: BTreeMap<Height, Vec<T>>,
}
```

**File:** rs/artifact_pool/src/height_index.rs (L30-37)
```rust
    pub fn insert(&mut self, height: Height, value: &T) -> bool {
        let values = self.buckets.entry(height).or_default();
        if !values.contains(value) {
            values.push(value.clone());
            return true;
        }
        false
    }
```

**File:** rs/artifact_pool/src/height_index.rs (L96-109)
```rust
pub struct Indexes {
    pub random_beacon: HeightIndex<CryptoHashOf<RandomBeacon>>,
    pub finalization: HeightIndex<CryptoHashOf<Finalization>>,
    pub notarization: HeightIndex<CryptoHashOf<Notarization>>,
    pub block_proposal: HeightIndex<CryptoHashOf<BlockProposal>>,
    pub random_beacon_share: HeightIndex<CryptoHashOf<RandomBeaconShare>>,
    pub notarization_share: HeightIndex<CryptoHashOf<NotarizationShare>>,
    pub finalization_share: HeightIndex<CryptoHashOf<FinalizationShare>>,
    pub random_tape: HeightIndex<CryptoHashOf<RandomTape>>,
    pub random_tape_share: HeightIndex<CryptoHashOf<RandomTapeShare>>,
    pub catch_up_package: HeightIndex<CryptoHashOf<CatchUpPackage>>,
    pub catch_up_package_share: HeightIndex<CryptoHashOf<CatchUpPackageShare>>,
    pub equivocation_proof: HeightIndex<CryptoHashOf<EquivocationProof>>,
}
```

**File:** rs/artifact_pool/src/inmemory_pool.rs (L30-35)
```rust
    fn insert(&mut self, artifact: T) {
        let msg = artifact.as_ref();
        let hash = msg.get_cm_hash().digest().clone();
        self.indexes.insert(msg, &hash);
        self.artifacts.entry(hash).or_insert(artifact);
    }
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L359-391)
```rust
        let peer_slot_table = self.slot_table.entry(peer_id).or_default();
        let peer_slot_table_len = peer_slot_table.len();
        let (to_add, to_remove) = match peer_slot_table.entry(slot_number) {
            Entry::Occupied(mut slot_entry_mut) => {
                if slot_entry_mut.get().should_be_replaced(&new_slot_entry) {
                    self.metrics.slot_table_overwrite_total.inc();
                    let to_remove = slot_entry_mut.insert(new_slot_entry).id;
                    (true, Some(to_remove))
                } else {
                    self.metrics.slot_table_stale_total.inc();
                    (false, None)
                }
            }
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
