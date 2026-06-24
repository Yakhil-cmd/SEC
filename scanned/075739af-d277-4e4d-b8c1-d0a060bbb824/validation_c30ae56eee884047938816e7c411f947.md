### Title
Unbounded DKG Unvalidated Pool Growth via Byzantine P2P Peer — (`rs/artifact_pool/src/dkg_pool.rs`)

---

### Summary

`DkgPoolImpl::insert` accepts artifacts into an unbounded in-memory `BTreeMap` with no capacity check. The P2P slot table for DKG is configured with `SLOT_TABLE_NO_LIMIT = usize::MAX`, and the `DkgBouncer` only filters on block height — not on dealer identity or signature validity. A single Byzantine subnet node can flood the unvalidated pool with syntactically valid, height-correct DKG messages carrying distinct dealing payloads, each producing a unique `DkgMessageId` hash, exhausting replica memory before the next purge cycle.

---

### Finding Description

**1. No size bound in `DkgPoolImpl::insert`** [1](#0-0) 

The insert is a direct, unchecked `BTreeMap` insertion. There is no capacity limit, no per-peer quota, and no rejection path.

**2. DKG slot table is configured with `usize::MAX`** [2](#0-1) [3](#0-2) 

Compare this to ingress, which uses `SLOT_TABLE_LIMIT_INGRESS = 50_000` per peer. DKG has no such protection. The per-peer slot table check at `receiver.rs:373` (`peer_slot_table_len < self.slot_limit`) is effectively disabled. [4](#0-3) 

**3. `DkgBouncer` only checks block height — not dealer membership or signature** [5](#0-4) 

A Byzantine node can craft messages with `id.height == start_height` (the current DKG interval start), which returns `BouncerValue::Wants`. The bouncer does not check whether the signer is a registered dealer, whether the signature is valid, or whether the dealing content is well-formed. All such messages are fetched and inserted.

**4. `DkgMessageId` is a crypto hash of the full message** [6](#0-5) 

Each distinct `NiDkgDealing` payload produces a unique `DkgMessageId`. A Byzantine node can generate an unbounded stream of unique IDs by varying the dealing bytes, each occupying a distinct slot and a distinct pool entry.

**5. Validation is asynchronous and only partially cleans up** [7](#0-6) 

`on_state_change` groups unvalidated messages by `(dealer, DKG ID)` and validates only the first per group. Messages with an invalid dealer or bad signature are removed, but only after they have already been inserted. If the Byzantine node sends faster than the validator runs, the pool grows unboundedly. The purge only removes messages from *previous* DKG intervals: [8](#0-7) 

Messages at the *current* interval height accumulate until the next summary block.

---

### Impact Explanation

A single Byzantine subnet node (within the f-fault model) can exhaust the heap of every honest replica it is connected to. `NiDkgDealing` messages contain large cryptographic blobs. Sending 10^4–10^5 unique messages at the current DKG interval height will grow the unvalidated pool to gigabytes before the next purge. The replica process is killed by the OS OOM killer, halting its participation in consensus and potentially stalling the subnet if enough replicas are affected simultaneously.

---

### Likelihood Explanation

The attacker must be a registered subnet node with a valid TLS certificate — not a completely external party. However, a single compromised node is within the IC Byzantine fault model (f out of 3f+1). The attack requires no special preconditions beyond being a P2P peer at the current DKG interval. The `DkgBouncer` height check is trivially satisfied by using the correct `start_block_height`. The slot table imposes no limit. The attack is local-testable and mechanically straightforward.

---

### Recommendation

1. **Add a per-peer slot limit for DKG** analogous to `SLOT_TABLE_LIMIT_INGRESS`. Replace `SLOT_TABLE_NO_LIMIT` with a registry-derived constant (e.g., `num_dealers * num_dkg_configs * small_multiplier`).

2. **Add a capacity check in `DkgPoolImpl::insert`** that rejects insertions beyond a computed bound (e.g., `num_subnet_nodes * num_active_dkg_configs * MAX_DEALINGS_PER_DEALER`).

3. **Extend `DkgBouncer` to check dealer membership** against the current DKG summary before returning `BouncerValue::Wants`, so messages from non-dealers are dropped at the P2P layer before pool insertion.

---

### Proof of Concept

```rust
// Byzantine node: generate 100_000 unique DKG messages at the current interval height
for i in 0..100_000u64 {
    let dealing = NiDkgDealing { internal_dealing: craft_unique_dealing(i) };
    let content = DealingContent::new(dealing, current_dkg_id); // height == start_height
    let msg = sign_with_byzantine_key(content);
    p2p_send_slot_update(msg); // passes DkgBouncer (height == start_height → Wants)
    // Each msg has a unique DkgMessageId (crypto_hash of full message)
    // SLOT_TABLE_NO_LIMIT → slot accepted
    // DkgPoolImpl::insert → no capacity check → pool grows
}
// Replica heap exhausted; OOM kill; consensus participation halted
```

The `DkgBouncer` returns `Wants` for any message whose `id.height == dkg_pool.get_current_start_height()`. [9](#0-8) 

The pool insert is unconditional. [10](#0-9)

### Citations

**File:** rs/artifact_pool/src/dkg_pool.rs (L59-82)
```rust
    fn purge(&mut self, height: Height) -> Vec<DkgMessageId> {
        self.current_start_height = height;
        // TODO: use drain_filter once it's stable.
        let unvalidated_keys: Vec<_> = self
            .unvalidated
            .keys()
            .filter(|id| id.height < height)
            .cloned()
            .collect();
        for id in unvalidated_keys {
            self.unvalidated.remove(&id);
        }

        let validated_keys: Vec<_> = self
            .validated
            .keys()
            .filter(|id| id.height < height)
            .cloned()
            .collect();
        for hash in &validated_keys {
            self.validated.remove(hash);
        }
        validated_keys
    }
```

**File:** rs/artifact_pool/src/dkg_pool.rs (L88-92)
```rust
    /// Inserts an unvalidated artifact into the unvalidated section.
    fn insert(&mut self, artifact: UnvalidatedArtifact<consensus::dkg::Message>) {
        self.unvalidated
            .insert(DkgMessageId::from(&artifact.message), artifact);
    }
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L74-75)
```rust
const SLOT_TABLE_LIMIT_INGRESS: usize = 50_000;
const SLOT_TABLE_NO_LIMIT: usize = usize::MAX;
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L271-280)
```rust
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

**File:** rs/consensus/dkg/src/lib.rs (L339-368)
```rust
        let mut processed = 0;
        let dealings: Vec<Vec<&Message>> = dkg_pool
            .get_unvalidated()
            // Group all unvalidated dealings by (dealer, DKG ID).
            .fold(BTreeMap::new(), |mut map, dealing| {
                let key = (dealing.signature.signer, dealing.content.dkg_id.clone());
                let dealings: &mut Vec<_> = map.entry(key).or_default();
                dealings.push(dealing);
                processed += 1;
                map
            })
            // Get the dealings sorted by (dealer, DKG ID)
            .into_values()
            .collect();

        let changeset = dealings
            .par_iter()
            .map(|dealings| {
                self.validate_dealings_for_dealer(dkg_pool, &configs, start_height, dealings)
            })
            .collect::<Vec<Mutations>>()
            .into_iter()
            .flatten()
            .collect::<Mutations>();

        self.metrics
            .on_state_change_processed
            .observe(processed as f64);
        changeset
    }
```

**File:** rs/consensus/dkg/src/lib.rs (L391-408)
```rust
impl<Pool: DkgPool> BouncerFactory<DkgMessageId, Pool> for DkgBouncer {
    fn new_bouncer(&self, dkg_pool: &Pool) -> Bouncer<DkgMessageId> {
        let _timer = self.metrics.update_duration.start_timer();

        let start_height = dkg_pool.get_current_start_height();
        Box::new(move |id| {
            use std::cmp::Ordering;
            match id.height.cmp(&start_height) {
                Ordering::Equal => BouncerValue::Wants,
                Ordering::Greater => BouncerValue::MaybeWantsLater,
                Ordering::Less => BouncerValue::Unwanted,
            }
        })
    }

    fn refresh_period(&self) -> std::time::Duration {
        std::time::Duration::from_secs(3)
    }
```

**File:** rs/types/types/src/consensus/dkg.rs (L48-55)
```rust
impl From<&Message> for DkgMessageId {
    fn from(msg: &Message) -> Self {
        Self {
            hash: crypto_hash(msg),
            height: msg.content.dkg_id.start_block_height,
        }
    }
}
```
