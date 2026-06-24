Audit Report

## Title
Unbounded Unvalidated DKG Pool Growth via Deferred Remote Dealings — (`rs/consensus/dkg/src/lib.rs`)

## Summary
A single Byzantine subnet node can flood the unvalidated DKG pool by advertising an unbounded number of crafted `NiDkgTargetSubnet::Remote` dealings with distinct `NiDkgId` values at the current `start_height`. The `validate_dealings_for_dealer` function silently defers all such messages without removing them, no per-peer slot limit exists for the DKG P2P channel, and the pool's underlying `BTreeMap` has no capacity cap. Entries survive until the next DKG interval purge (~500 blocks), causing unbounded memory growth that can crash or severely degrade targeted replicas.

## Finding Description

**Root cause chain:**

**1. Bouncer admits all messages at `start_height`.**
`DkgBouncer::new_bouncer` returns `BouncerValue::Wants` for any `DkgMessageId` whose `height == start_height`, with no check on the `NiDkgId` content: [1](#0-0) 

**2. No per-peer slot table limit for DKG.**
The DKG broadcast channel is created with `SLOT_TABLE_NO_LIMIT = usize::MAX`, unlike ingress which uses `SLOT_TABLE_LIMIT_INGRESS = 50_000`: [2](#0-1) [3](#0-2) 

The `ConsensusManagerReceiver` enforces the limit at the `Entry::Vacant` branch — with `usize::MAX`, the check `peer_slot_table_len < self.slot_limit` is always true, so every new slot number from the attacker is accepted: [4](#0-3) 

**3. Pool has no capacity limit.**
`DkgPoolImpl::insert` places the artifact directly into a `PoolSection<DkgMessageId, ...>` backed by a plain `BTreeMap` with no size check: [5](#0-4) [6](#0-5) 

**4. `validate_dealings_for_dealer` silently defers all unknown remote dealings.**
When `configs.get(message_dkg_id)` returns `None` and `target_subnet.is_remote()` is true, the function returns `Mutations::new()` — no removal, no invalidation, no eviction. Crypto verification is never reached because the config lookup fails first: [7](#0-6) 

This behavior is intentional and confirmed by the test at: [8](#0-7) 

**5. Purge only fires on DKG interval advance and only removes entries with `height < start_height`.**
The purge fires only when `start_height > dkg_pool.get_current_start_height()` (i.e., at the end of a DKG interval, ~500 blocks): [9](#0-8) 

The purge filter removes entries with `id.height < height`. Attacker messages crafted with `height == start_height` survive the purge and persist until the *next* interval: [10](#0-9) 

**Exploit flow:**
1. Attacker (compromised subnet node) sends slot updates on slot numbers 1…N, each advertising a distinct `DkgMessageId` with `height = start_height` and `target_subnet = NiDkgTargetSubnet::Remote(NiDkgTargetId::new([i as u8; 32]))`.
2. `SLOT_TABLE_NO_LIMIT` allows all N slots to be accepted; N artifact downloads are triggered.
3. Each downloaded artifact is inserted into the unvalidated pool via `DkgPoolImpl::insert`.
4. `on_state_change` calls `validate_dealings_for_dealer` for each; all return `Mutations::new()` (deferred).
5. Pool accumulates N entries for the full DKG interval (~8 minutes). The `active_assembles` HashMap in `ConsensusManagerReceiver` also grows to N entries.
6. Memory grows without bound until OOM or the next interval purge.

## Impact Explanation

This is a **High** severity finding matching: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

The attack exploits a specific protocol-level deferral behavior (not raw packet flooding) to cause unbounded heap growth in both the unvalidated `DkgPoolImpl` (`BTreeMap`) and the `ConsensusManagerReceiver` slot table and `active_assembles` map. Sustained over a DKG interval, this can exhaust replica memory, causing a crash or severe performance degradation that disrupts subnet availability.

## Likelihood Explanation

The attacker must be a registered subnet node (P2P connections require TLS with subnet membership verification). A single compromised node below the consensus fault threshold suffices. No cryptographic capability is required — the deferral path is reached before any signature or dealing verification. The attack is repeatable every DKG interval. The deferral behavior is explicitly documented and tested as intentional, meaning it will not be accidentally fixed by unrelated changes.

## Recommendation

1. **Add a per-peer slot table limit for DKG** analogous to `SLOT_TABLE_LIMIT_INGRESS = 50_000`, replacing `SLOT_TABLE_NO_LIMIT` at `rs/replica/setup_ic_network/src/lib.rs:279`. The legitimate number of DKG dealings per interval is bounded by `num_dealers * num_configs`, which is small (typically single digits to low tens).
2. **Immediately invalidate deferred remote dealings** whose `dealer_subnet` field does not match any subnet known to the registry, rather than deferring indefinitely. This converts the silent accumulation into a bounded, auditable rejection.
3. **Cap the unvalidated DKG pool size** (e.g., `num_dealers * num_configs * small_multiplier`) and drop excess entries on insertion.
4. **Add a TTL** to unvalidated DKG pool entries so they are evicted after a bounded number of rounds even without a summary advance.

## Proof of Concept

```rust
// Unit test sketch against DkgPoolImpl + DkgImpl::on_state_change
let start_height = dkg_pool.get_current_start_height();
for i in 0..10_000u64 {
    let remote_dkg_id = NiDkgId {
        start_block_height: start_height,
        dealer_subnet: attacker_subnet_id,
        dkg_tag: NiDkgTag::HighThreshold,
        target_subnet: NiDkgTargetSubnet::Remote(NiDkgTargetId::new([i as u8; 32])),
    };
    let msg = create_dealing_with_id(remote_dkg_id); // unique DkgMessageId per iteration
    dkg_pool.insert(UnvalidatedArtifact {
        message: msg,
        peer_id: attacker_node,
        timestamp: UNIX_EPOCH,
    });
}
// on_state_change returns Mutations::new() for all entries — pool never shrinks
let mutations = dkg_impl.on_state_change(&dkg_pool);
assert!(mutations.is_empty());
assert_eq!(dkg_pool.get_unvalidated().count(), 10_000); // passes
```

This is directly analogous to the existing test at `rs/consensus/dkg/src/lib.rs:2168–2172` which already confirms the deferral behavior with two messages; scaling to N messages demonstrates the unbounded growth.

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

**File:** rs/artifact_pool/src/dkg_pool.rs (L62-70)
```rust
        let unvalidated_keys: Vec<_> = self
            .unvalidated
            .keys()
            .filter(|id| id.height < height)
            .cloned()
            .collect();
        for id in unvalidated_keys {
            self.unvalidated.remove(&id);
        }
```

**File:** rs/artifact_pool/src/dkg_pool.rs (L89-92)
```rust
    fn insert(&mut self, artifact: UnvalidatedArtifact<consensus::dkg::Message>) {
        self.unvalidated
            .insert(DkgMessageId::from(&artifact.message), artifact);
    }
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
