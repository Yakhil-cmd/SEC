Based on my code investigation, here is the assessment:

---

### Title
Unbounded DKG Unvalidated Pool Growth via Byzantine Peer Flooding with Remote DKG Dealings — (`rs/consensus/dkg/src/lib.rs`, `rs/artifact_pool/src/dkg_pool.rs`)

### Summary
A single Byzantine subnet node can flood the DKG unvalidated pool with dealings referencing non-existent remote DKG target IDs. The `validate_dealings_for_dealer` function explicitly defers (returns `Mutations::new()`) rather than rejecting such dealings, the pool has no size cap, and the P2P slot table for DKG is set to `SLOT_TABLE_NO_LIMIT`. Deferred dealings accumulate for an entire DKG interval (~499 rounds) before being purged, enabling memory exhaustion on victim replicas.

### Finding Description

**Root cause 1 — Unconditional deferral without signature or size guard:**

In `validate_dealings_for_dealer`, when a dealing's `dkg_id.target_subnet.is_remote()` is true and no matching config exists, the function returns `Mutations::new()` (skip) without verifying the signature, without removing the artifact, and without any rate-limit check: [1](#0-0) 

The comment even documents this as intentional: *"we defer it until the request appears in the state, or the dealing is purged."*

**Root cause 2 — No pool size limit:**

`DkgPoolImpl.insert` unconditionally inserts into a `BTreeMap`-backed `PoolSection` with no maximum capacity: [2](#0-1) 

There is no `max_count`, `max_bytes`, or per-peer counter analogous to the ingress pool's `exceeds_limit` check.

**Root cause 3 — No P2P slot table limit for DKG:**

The DKG artifact channel is registered with `SLOT_TABLE_NO_LIMIT = usize::MAX`, meaning a single peer can advertise an unlimited number of DKG artifacts: [3](#0-2) 

Compare this to ingress, which uses `SLOT_TABLE_LIMIT_INGRESS = 50_000`: [4](#0-3) 

**Root cause 4 — DkgBouncer only filters by height, not by DKG ID validity:**

The bouncer accepts any artifact whose `height == current_start_height`, regardless of whether the `target_subnet` is a known remote ID: [5](#0-4) 

**Root cause 5 — Purge only triggers on DKG interval advance:**

The pool is only purged when `start_height > dkg_pool.get_current_start_height()`, i.e., when a new summary block is finalized. With `DKG_INTERVAL_HEIGHT = 499`, deferred artifacts can accumulate for up to 499 consensus rounds: [6](#0-5) [7](#0-6) 

### Impact Explanation

A Byzantine subnet node (single node, below fault threshold) can:
1. Generate N unique DKG dealings with `start_block_height = current_start_height` and `target_subnet = Remote(random_id_i)` for i = 1..N, signing each with its own valid node key.
2. Advertise all N via P2P (no slot table limit).
3. Victim replicas download and insert all N into the unvalidated pool (no size limit).
4. `validate_dealings_for_dealer` defers all N (no matching config, remote target).
5. Pool grows to N entries, consuming O(N × dealing_size) memory.
6. This persists for up to 499 rounds until the next DKG interval purge.

Each NiDKG dealing is a substantial cryptographic object (kilobytes to megabytes depending on subnet size). At scale, this causes replica OOM or severe GC pressure, degrading consensus throughput or causing a subnet halt if enough replicas are affected.

The `on_state_change` loop also iterates over all unvalidated dealings on every call, so a large pool also causes CPU exhaustion proportional to pool size: [8](#0-7) 

### Likelihood Explanation

- Requires only a single compromised subnet node (well within the Byzantine fault model).
- No cryptographic capability beyond the node's own signing key is needed.
- The attack is trivially automatable: generate unique dealing content (arbitrary bytes), sign, advertise.
- The existing test `test_remote_dealing_validation_is_deferred_until_context_exists` confirms the deferral behavior is exercised in practice. [9](#0-8) 

### Recommendation

1. **Reject remote DKG dealings with unrecognized target IDs after signature verification.** Once the signature is verified (proving the sender is a legitimate subnet node), dealings for unknown remote targets should be `HandleInvalid`-ed rather than deferred indefinitely. Deferral should only apply when the target ID is plausibly expected (e.g., it appears in a pending `SetupInitialDkg` context in the state).
2. **Add a pool size cap to `DkgPoolImpl`**, analogous to `ingress_pool_max_count`/`ingress_pool_max_bytes`.
3. **Apply a per-peer slot table limit for DKG** (replace `SLOT_TABLE_NO_LIMIT` with a bounded constant, e.g., `2 × max_dealers_per_subnet`).
4. **Verify the signature before deferring** remote DKG dealings, so only authenticated subnet nodes can contribute deferred entries.

### Proof of Concept

```
1. Identify current DKG start_height H from the consensus pool (public).
2. For i in 1..N:
   a. Construct DealingContent { dealing: random_bytes, dkg_id: NiDkgId {
        start_block_height: H,
        target_subnet: Remote(NiDkgTargetId::new(random_32_bytes_i)),
        ... } }
   b. Sign with Byzantine node's key → Message { content, signature }
   c. Advertise via P2P to victim replicas
3. Observe victim replica memory growing by N × dealing_size.
4. Assert pool size is unbounded (no rejection, no eviction until next summary block).
```

### Citations

**File:** rs/consensus/dkg/src/lib.rs (L204-219)
```rust
        // If the dealing refers a config which is not among the ongoing DKGs,
        // we reject it, unless it is a remote DKG, in which case we defer it
        // until the request appears in the state, or the dealing is purged.
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

**File:** rs/consensus/dkg/src/lib.rs (L339-362)
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

**File:** rs/consensus/dkg/src/lib.rs (L2093-2172)
```rust
    fn test_remote_dealing_validation_is_deferred_until_context_exists() {
        ic_test_utilities::artifact_pool_config::with_test_pool_config(|pool_config| {
            with_test_replica_logger(|logger| {
                let node_ids = vec![node_test_id(0), node_test_id(1)];
                let dkg_interval_length = 99;
                let subnet_id = subnet_test_id(0);
                let target_id = NiDkgTargetId::new([9_u8; 32]);

                let mut deps = dependencies_with_subnet_records_with_raw_state_manager(
                    pool_config,
                    subnet_id,
                    vec![(
                        10,
                        SubnetRecordBuilder::from(&node_ids)
                            .with_dkg_interval_length(dkg_interval_length)
                            .build(),
                    )],
                );

                // Start without context so remote dealing validation is deferred.
                complement_state_manager_with_dkg_contexts(
                    deps.state_manager.clone(),
                    vec![],
                    None,
                );
                deps.pool
                    .advance_round_normal_operation_n(dkg_interval_length + 1);

                // Non-dealer receiver: validates incoming dealings but does not create its own.
                let receiver_key_manager = new_dkg_key_manager(
                    deps.crypto.clone(),
                    logger.clone(),
                    &PoolReader::new(&deps.pool),
                );
                let receiver_dkg = DkgImpl::new(
                    node_test_id(2),
                    deps.replica_config.subnet_id,
                    deps.registry.clone(),
                    deps.state_manager.clone(),
                    deps.crypto.clone(),
                    deps.pool.get_cache(),
                    receiver_key_manager.clone(),
                    MetricsRegistry::new(),
                    logger,
                );

                let start_height = deps.pool.get_cache().summary_block().height;
                let mut dkg_pool =
                    DkgPoolImpl::new(MetricsRegistry::new(), no_op_logger(), start_height);
                let remote_dkg_id = NiDkgId {
                    start_block_height: start_height,
                    dealer_subnet: subnet_id,
                    dkg_tag: NiDkgTag::LowThreshold,
                    target_subnet: NiDkgTargetSubnet::Remote(target_id),
                };
                let remote_message = create_dealing(1, remote_dkg_id);
                let other_target_id = NiDkgTargetId::new([10_u8; 32]);
                let deferred_remote_dkg_id = NiDkgId {
                    start_block_height: start_height,
                    dealer_subnet: subnet_id,
                    dkg_tag: NiDkgTag::LowThreshold,
                    target_subnet: NiDkgTargetSubnet::Remote(other_target_id),
                };
                let deferred_remote_message = create_dealing(42, deferred_remote_dkg_id);
                dkg_pool.insert(UnvalidatedArtifact {
                    message: remote_message.clone(),
                    peer_id: node_test_id(1),
                    timestamp: ic_types::time::UNIX_EPOCH,
                });
                dkg_pool.insert(UnvalidatedArtifact {
                    message: deferred_remote_message,
                    peer_id: node_test_id(42),
                    timestamp: ic_types::time::UNIX_EPOCH,
                });

                assert!(
                    receiver_dkg.on_state_change(&dkg_pool).is_empty(),
                    "dealing should be deferred while context is missing",
                );
                assert_eq!(dkg_pool.get_unvalidated().count(), 2);
```

**File:** rs/artifact_pool/src/dkg_pool.rs (L88-92)
```rust
    /// Inserts an unvalidated artifact into the unvalidated section.
    fn insert(&mut self, artifact: UnvalidatedArtifact<consensus::dkg::Message>) {
        self.unvalidated
            .insert(DkgMessageId::from(&artifact.message), artifact);
    }
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

**File:** rs/limits/src/lib.rs (L90-90)
```rust
pub const DKG_INTERVAL_HEIGHT: u64 = 499;
```
