Audit Report

## Title
Unbounded Unvalidated DKG Pool Growth via Byzantine P2P Peer — (`rs/artifact_pool/src/dkg_pool.rs`)

## Summary
`DkgPoolImpl::insert` performs an unconditional `BTreeMap` insertion with no capacity bound. The DKG P2P channel is configured with `SLOT_TABLE_NO_LIMIT = usize::MAX`, disabling the per-peer slot table guard that protects other artifact types. The `DkgBouncer` accepts any message whose `id.height == start_height` without checking dealer membership or signature validity, allowing a single Byzantine subnet node to flood the unvalidated pool with unique, height-correct DKG messages, exhausting replica heap memory before the next purge cycle.

## Finding Description

**Root cause 1 — No capacity check in `DkgPoolImpl::insert`.** [1](#0-0) 
The insertion is unconditional. There is no per-peer quota, no total pool size limit, and no rejection path.

**Root cause 2 — DKG slot table limit is `usize::MAX`.** [2](#0-1) [3](#0-2) 
The per-peer slot table guard at `receiver.rs:373` (`peer_slot_table_len < self.slot_limit`) is effectively disabled for DKG. Compare to ingress, which uses `SLOT_TABLE_LIMIT_INGRESS = 50_000`. [4](#0-3) 

**Root cause 3 — `DkgBouncer` only checks block height.** [5](#0-4) 
Any message with `id.height == start_height` returns `BouncerValue::Wants`. Dealer membership, signature validity, and dealing well-formedness are not checked at the P2P layer.

**Root cause 4 — `DkgMessageId` is a crypto hash of the full message.** [6](#0-5) 
Varying any byte of the `NiDkgDealing` payload produces a distinct `DkgMessageId`, so each crafted message occupies a unique pool entry and a unique slot.

**Root cause 5 — Validation is asynchronous and processes only one message per `(dealer, DKG ID)` group per cycle.** [7](#0-6) [8](#0-7) 
Only `messages.first()` is validated per group per `on_state_change` call. If the Byzantine node uses its own node ID as the dealer, all N flooded messages land in one group and are cleaned up one per cycle — O(N) cycles to drain the pool.

**Root cause 6 — Purge only removes messages from previous DKG intervals.** [9](#0-8) 
The filter is `id.height < height` (strict less-than). Messages at the current interval height accumulate until the next summary block advances the purge height.

**Exploit flow:**
1. Byzantine node (valid TLS cert, registered subnet peer) crafts N DKG messages with `dkg_id.start_block_height == current_start_height` and distinct `NiDkgDealing` payloads, using N distinct slot numbers.
2. Each slot update passes the slot table check (`peer_slot_table_len < usize::MAX` — always true).
3. `DkgBouncer` returns `Wants` for each (height matches).
4. Each message is fetched and inserted via `DkgPoolImpl::insert` — no capacity check.
5. Validation runs asynchronously; if the Byzantine node's node ID is not a registered dealer, the first message per group is invalidated per cycle, but the remaining N−1 messages remain in the pool.
6. The unvalidated pool holds N large `NiDkgDealing` blobs in memory until cleanup catches up or the next summary block arrives.

## Impact Explanation
A single Byzantine subnet node can exhaust the heap of every honest replica it is connected to. `NiDkgDealing` messages contain large cryptographic blobs (kilobytes each). Sending 10^4–10^5 unique messages at the current DKG interval height grows the unvalidated pool to gigabytes before the next purge. The replica process is killed by the OS OOM killer, halting its participation in consensus. If enough replicas are simultaneously affected, subnet liveness is lost. This matches the allowed impact: **Application/platform-level DoS, crash, consensus blocking, or subnet availability impact not based on raw volumetric DDoS** — **High ($2,000–$10,000)**.

## Likelihood Explanation
The attacker must be a registered subnet node with a valid TLS certificate — not a fully external party. However, a single compromised node is within the IC Byzantine fault model (f out of 3f+1). The `DkgBouncer` height check is trivially satisfied by using the correct `start_block_height`. The slot table imposes no limit. No special preconditions beyond being a P2P peer at the current DKG interval are required. The attack is mechanically straightforward and repeatable across DKG intervals.

## Recommendation
1. **Add a per-peer slot limit for DKG** analogous to `SLOT_TABLE_LIMIT_INGRESS`. Replace `SLOT_TABLE_NO_LIMIT` with a registry-derived constant (e.g., `num_dealers × num_dkg_configs × small_multiplier`) in `rs/replica/setup_ic_network/src/lib.rs`.
2. **Add a capacity check in `DkgPoolImpl::insert`** that rejects insertions beyond a computed bound (e.g., `num_subnet_nodes × num_active_dkg_configs × MAX_DEALINGS_PER_DEALER`).
3. **Extend `DkgBouncer` to check dealer membership** against the current DKG summary before returning `BouncerValue::Wants`, so messages from non-dealers are dropped at the P2P layer before pool insertion.

## Proof of Concept
```rust
// Byzantine node: generate N unique DKG messages at the current interval height
for i in 0..100_000u64 {
    // Vary dealing bytes to produce a unique DkgMessageId (crypto_hash of full message)
    let dealing = NiDkgDealing { internal_dealing: craft_unique_dealing(i) };
    let content = DealingContent::new(dealing, current_dkg_id); // height == start_height
    let msg = sign_with_byzantine_key(content);
    // Advertise via a new slot number i → passes SLOT_TABLE_NO_LIMIT check
    p2p_send_slot_update(slot=i, msg);
    // DkgBouncer: id.height == start_height → BouncerValue::Wants → fetched
    // DkgPoolImpl::insert: unconditional BTreeMap insert → pool grows
}
// Replica heap exhausted; OOM kill; consensus participation halted
```

A deterministic integration test can be written using `PocketIC` or a local replica harness: inject N crafted `UnvalidatedArtifact<dkg::Message>` entries directly into `DkgPoolImpl` via `MutablePool::insert`, assert pool size equals N, then assert that a single `on_state_change` call removes at most 1 entry per `(dealer, DKG ID)` group, confirming O(N) cleanup latency.

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

**File:** rs/consensus/dkg/src/lib.rs (L186-190)
```rust
        let message = if let Some(message) = messages.first() {
            message
        } else {
            return Mutations::new();
        };
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

**File:** rs/consensus/dkg/src/lib.rs (L391-409)
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
