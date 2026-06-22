Based on my analysis of the actual production code, I can now render a verdict.

**Key code facts established:**

1. **The deferral branch is confirmed** — `validate_dealings_for_dealer` at line 209 returns `Mutations::new()` (no removal) for any remote-targeted dealing with no matching config. [1](#0-0) 

2. **`DkgPoolImpl` has zero size limit** — `insert()` performs no admission check whatsoever. [2](#0-1) 

3. **The DKG slot table limit is `usize::MAX`** — DKG is registered with `SLOT_TABLE_NO_LIMIT`, unlike ingress which uses `SLOT_TABLE_LIMIT_INGRESS = 50_000`. [3](#0-2) [4](#0-3) 

4. **`DkgBouncer` only checks height** — it returns `BouncerValue::Wants` for any message whose `id.height == current_start_height`, with no check on `target_subnet` validity or context existence. [5](#0-4) 

5. **Signature is never verified for deferred messages** — the crypto verification path (`crypto_validate_dealing`) is only reached after the config lookup succeeds; the deferral branch exits before it. [6](#0-5) 

6. **Purge only happens at interval boundary** — messages with `id.height < new_start_height` are removed only when `on_state_change` detects `start_height > pool.get_current_start_height()`. [7](#0-6) [8](#0-7) 

---

### Title
Unbounded DKG Unvalidated Pool Growth via Spoofed Remote-Target Dealings — (`rs/consensus/dkg/src/lib.rs`)

### Summary
A Byzantine subnet peer can flood the unvalidated DKG pool with arbitrarily many dealings carrying `NiDkgTargetSubnet::Remote(arbitrary_id)` and the correct `start_block_height`. Because the validation path silently defers these messages without removing them, and because neither the P2P slot table nor the pool itself imposes any size limit on DKG artifacts, the messages accumulate in memory for the entire DKG interval with no signature verification and no eviction.

### Finding Description
In `validate_dealings_for_dealer` (`rs/consensus/dkg/src/lib.rs`), when a dealing's `NiDkgId` is not found in the current config map **and** `target_subnet.is_remote()` is true, the function returns `Mutations::new()` — an empty change set — leaving the message in the unvalidated pool indefinitely:

```rust
let config = match configs.get(message_dkg_id) {
    Some(config) => config,
    None if message_dkg_id.target_subnet.is_remote() => {
        return Mutations::new();   // ← no removal, no rejection
    }
    ...
```

This is intentional for the legitimate case where a remote DKG context has not yet appeared in certified state. However, three missing guards make it exploitable:

- **No slot table limit for DKG.** The P2P layer registers DKG with `SLOT_TABLE_NO_LIMIT = usize::MAX`, so a single Byzantine peer can advertise an unlimited number of distinct artifact IDs, each occupying its own slot.
- **No pool size cap.** `DkgPoolImpl::insert()` performs no admission check; every downloaded artifact is stored unconditionally.
- **No signature verification before deferral.** The crypto path is only reached after a config is found; deferred messages bypass it entirely, so the attacker need not produce valid signatures.

An attacker crafts messages with distinct `NiDkgId` values (varying `target_subnet = Remote(random_32_bytes)`) and the correct `start_block_height`. Each message has a unique `CryptoHash`, so each occupies a separate pool entry. All pass the bouncer (height matches), all are downloaded, all are inserted, and all are deferred on every `on_state_change` call until the next DKG interval purge.

### Impact Explanation
Memory on the victim replica grows proportionally to the number of injected messages. A DKG dealing is non-trivial in size (contains NiDKG dealing ciphertext). Injecting hundreds of thousands of messages over a 500-block interval can exhaust heap memory and crash the replica process (OOM), causing it to fall behind consensus and potentially be excluded from the subnet.

### Likelihood Explanation
The attacker must be an authenticated subnet peer (Byzantine node below the fault threshold). This is within the stated threat model ("protocol peer behavior below the consensus fault threshold"). No cryptographic material needs to be forged — the signature field is never checked on the deferral path. The attack requires only network bandwidth to advertise and push many distinct artifacts.

### Recommendation
Apply at least one of the following mitigations:

1. **Reject unknown remote-target dealings outright** unless the `dealer_subnet` matches the local subnet and the `start_block_height` matches. If no context exists for the target, treat it as invalid rather than deferred.
2. **Cap the DKG slot table** analogously to ingress (`SLOT_TABLE_LIMIT_INGRESS = 50_000`), bounding per-peer DKG artifact injection.
3. **Add a pool size limit** in `DkgPoolImpl::insert()` that rejects new entries once a configurable maximum is reached.
4. **Verify the signature before deferring** — a dealing with an invalid signature should be `HandleInvalid`-ed regardless of whether a config exists.

### Proof of Concept
State-machine test (no network required):

```rust
// Inject N remote-targeted messages with no matching context
for i in 0..100_000u32 {
    let target_id = NiDkgTargetId::new(i.to_le_bytes().try_into().unwrap()); // unique per message
    let dkg_id = NiDkgId {
        start_block_height: start_height,
        dealer_subnet: subnet_id,
        dkg_tag: NiDkgTag::LowThreshold,
        target_subnet: NiDkgTargetSubnet::Remote(target_id),
    };
    dkg_pool.insert(UnvalidatedArtifact {
        message: create_dealing(i as u8, dkg_id),
        peer_id: node_test_id(1),
        timestamp: UNIX_EPOCH,
    });
}

// on_state_change defers all of them — pool is not bounded
for _ in 0..10 {
    let cs = dkg.on_state_change(&dkg_pool);
    assert!(cs.is_empty()); // no removals
}
assert_eq!(dkg_pool.get_unvalidated().count(), 100_000); // all still present
```

The existing test `test_remote_dealing_validation_is_deferred_until_context_exists` already confirms the deferral behavior with 2 messages; scaling to 100k demonstrates the unbounded growth. [9](#0-8)

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

**File:** rs/consensus/dkg/src/lib.rs (L2093-2212)
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

                // Add context back: deferred dealing should now be validated.
                deps.state_manager.get_mut().checkpoint();
                complement_state_manager_with_setup_initial_dkg_request(
                    deps.state_manager.clone(),
                    deps.registry.get_latest_version(),
                    vec![10, 11, 12],
                    None,
                    Some(target_id),
                );
                let change_set = receiver_dkg.on_state_change(&dkg_pool);
                match &change_set.as_slice() {
                    &[ChangeAction::MoveToValidated(message)] => {
                        assert_eq!(message.content.dkg_id, remote_message.content.dkg_id);
                        assert_eq!(
                            message.content.dkg_id.target_subnet,
                            NiDkgTargetSubnet::Remote(target_id)
                        );
                    }
                    val => panic!("Unexpected change set: {:?}", val),
                }
                dkg_pool.apply(change_set);
                assert_eq!(dkg_pool.get_validated().count(), 1);
                assert_eq!(dkg_pool.get_unvalidated().count(), 1);

                // Once the summary/start height advances, deferred unvalidated and old validated
                // dealings should be purged.
                deps.pool
                    .advance_round_normal_operation_n(dkg_interval_length + 1);
                let change_set = receiver_dkg.on_state_change(&dkg_pool);
                match &change_set.as_slice() {
                    &[ChangeAction::Purge(purge_height)] if *purge_height > start_height => {}
                    val => panic!("Expected purge after summary advance, got {:?}", val),
                }
                dkg_pool.apply(change_set);
                assert_eq!(dkg_pool.get_unvalidated().count(), 0);
                assert_eq!(dkg_pool.get_validated().count(), 0);
            });
        });
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
