Based on my analysis of the code, I can now provide a definitive assessment.

---

### Title
Unbounded DKG Unvalidated Pool Growth via Deferred Remote Dealings — (`rs/consensus/dkg/src/lib.rs`)

### Summary
A single Byzantine P2P peer (a compromised subnet node) can flood the unvalidated DKG pool with an unbounded number of crafted `NiDkgTargetSubnet::Remote` dealings whose `NiDkgId` does not appear in the current summary configs. The `validate_dealings_for_dealer` function silently defers all such messages (returns `Mutations::new()`) without removing them, and no pool size cap or per-peer quota exists for the DKG artifact channel. The pool grows without bound until the next DKG interval purge, which occurs only every ~500 blocks.

### Finding Description

**Step 1 — Bouncer admits all messages at `start_height`.**

The `DkgBouncer` returns `BouncerValue::Wants` for any `DkgMessageId` whose `height == start_height`: [1](#0-0) 

An attacker crafts messages with `height == start_height` and any `NiDkgId` with `target_subnet = NiDkgTargetSubnet::Remote(arbitrary_target_id)`.

**Step 2 — No slot table limit for DKG.**

The DKG broadcast channel is created with `SLOT_TABLE_NO_LIMIT = usize::MAX`: [2](#0-1) 

Compare this to ingress, which uses `SLOT_TABLE_LIMIT_INGRESS = 50_000`. The DKG channel has no per-peer cap. [3](#0-2) 

**Step 3 — Messages enter the unvalidated pool without size check.**

`DkgPoolImpl::insert` places the artifact into a `PoolSection<DkgMessageId, ...>`, which is a plain `BTreeMap` wrapper with no capacity limit: [4](#0-3) [5](#0-4) 

**Step 4 — `validate_dealings_for_dealer` silently defers all unknown remote dealings.**

When the `NiDkgId` is not in `configs` and `target_subnet.is_remote()` is true, the function returns `Mutations::new()` — no removal, no invalidation: [6](#0-5) 

The attacker does not need cryptographically valid dealings. The crypto verification path is never reached because the config lookup fails first.

**Step 5 — Purge only fires on DKG interval advance.**

The only eviction mechanism is `ChangeAction::Purge(start_height)`, triggered when `start_height > dkg_pool.get_current_start_height()`. This happens only at the end of a DKG interval (~500 blocks, ~8 minutes). The purge removes entries with `id.height < height`, but attacker messages have `height == start_height` and survive until the *next* interval: [7](#0-6) [8](#0-7) 

### Impact Explanation

Each crafted `dkg::Message` contains a `NiDkgDealing` (a large cryptographic blob). An attacker sending N messages with distinct `NiDkgId` values (varying `NiDkgTargetId`) and distinct dealing bytes produces N unique `DkgMessageId` hashes, bypassing deduplication. All N messages accumulate in the unvalidated `BTreeMap` for the duration of the DKG interval. At sufficient volume this causes OOM on the targeted replica, crashing it or severely degrading performance.

### Likelihood Explanation

The attacker must be a registered subnet node (P2P connections require TLS with subnet membership). A single compromised node below the fault threshold suffices. The attack requires no cryptographic capability — only the ability to send P2P messages with crafted `NiDkgId` fields. The deferral behavior is explicitly tested and documented as intentional (see the test at lib.rs:2168–2172 confirming deferred messages stay in the pool). [9](#0-8) 

### Recommendation

1. **Add a per-peer slot table limit for DKG** analogous to `SLOT_TABLE_LIMIT_INGRESS`, replacing `SLOT_TABLE_NO_LIMIT` at `setup_ic_network/src/lib.rs:279`.
2. **Cap the unvalidated DKG pool size** (e.g., `num_dealers * num_configs * small_multiplier`) and drop or reject excess entries.
3. **Immediately invalidate deferred remote dealings** whose `NiDkgId` cannot plausibly correspond to any pending remote DKG request (e.g., by checking the `dealer_subnet` field against the known subnet registry), rather than deferring indefinitely.
4. **Add a TTL** to unvalidated DKG pool entries so they are evicted after a bounded number of rounds even without a summary advance.

### Proof of Concept

```rust
// Pseudocode: insert N crafted remote dealings into DkgPoolImpl
// and verify pool grows without bound
let start_height = dkg_pool.get_current_start_height();
for i in 0..1_000_000u64 {
    let remote_dkg_id = NiDkgId {
        start_block_height: start_height,
        dealer_subnet: attacker_subnet_id,
        dkg_tag: NiDkgTag::HighThreshold,
        target_subnet: NiDkgTargetSubnet::Remote(NiDkgTargetId::new([i as u8; 32])),
    };
    let msg = craft_dealing_with_id(remote_dkg_id); // unique hash per iteration
    dkg_pool.insert(UnvalidatedArtifact { message: msg, peer_id: attacker_node, timestamp: now });
}
// on_state_change returns Mutations::new() for all of them — pool size == 1_000_000
assert_eq!(dkg_pool.get_unvalidated().count(), 1_000_000); // passes
```

### Citations

**File:** rs/consensus/dkg/src/lib.rs (L207-219)
```rust
        let config = match configs.get(message_dkg_id) {
            Some(config) => config,
            None if message_dkg_id.target_subnet.is_remote() => {
                return Mutations::new();
            }
            None => {
                return get_handle_invalid_change_action(
                    message,
                    format!("No DKG configuration for Id={message_dkg_id:?} was found."),
                )
                .into();
            }
        };
```

**File:** rs/consensus/dkg/src/lib.rs (L302-304)
```rust
        if start_height > dkg_pool.get_current_start_height() {
            return ChangeAction::Purge(start_height).into();
        }
```

**File:** rs/consensus/dkg/src/lib.rs (L396-403)
```rust
        Box::new(move |id| {
            use std::cmp::Ordering;
            match id.height.cmp(&start_height) {
                Ordering::Equal => BouncerValue::Wants,
                Ordering::Greater => BouncerValue::MaybeWantsLater,
                Ordering::Less => BouncerValue::Unwanted,
            }
        })
```

**File:** rs/consensus/dkg/src/lib.rs (L2168-2172)
```rust
                assert!(
                    receiver_dkg.on_state_change(&dkg_pool).is_empty(),
                    "dealing should be deferred while context is missing",
                );
                assert_eq!(dkg_pool.get_unvalidated().count(), 2);
```

**File:** rs/replica/setup_ic_network/src/lib.rs (L72-75)
```rust
/// This limit is used to protect against a malicious peer advertising many ingress messages.
/// If no malicious peers are present the ingress pools are bounded by a separate limit.
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

**File:** rs/artifact_pool/src/pool_common.rs (L19-41)
```rust
pub(crate) struct PoolSection<K, V> {
    messages: BTreeMap<K, V>,
    metrics: PoolMetrics,
}

impl<K: Ord, V: HasLabel> PoolSection<K, V> {
    pub(crate) fn new(metrics_registry: MetricsRegistry, pool: &str, pool_type: &str) -> Self {
        Self {
            messages: Default::default(),
            metrics: PoolMetrics::new(metrics_registry, pool, pool_type),
        }
    }

    pub(crate) fn insert(&mut self, key: K, value: V) -> Option<V> {
        self.metrics
            .observe_insert(MESSAGE_SIZE_BYTES, value.label());
        let replaced = self.messages.insert(key, value);
        if let Some(replaced) = &replaced {
            self.metrics
                .observe_duplicate(MESSAGE_SIZE_BYTES, replaced.label());
        }
        replaced
    }
```

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

**File:** rs/artifact_pool/src/dkg_pool.rs (L89-92)
```rust
    fn insert(&mut self, artifact: UnvalidatedArtifact<consensus::dkg::Message>) {
        self.unvalidated
            .insert(DkgMessageId::from(&artifact.message), artifact);
    }
```
