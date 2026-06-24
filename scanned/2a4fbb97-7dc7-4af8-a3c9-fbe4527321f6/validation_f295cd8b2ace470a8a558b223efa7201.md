Looking at the two key functions in `pool_manager.rs` and the `insert` path in `canister_http_pool.rs`:

The code confirms the vulnerability. Let me trace the exact logic:

**`validate_shares`** (line 484): skips all artifacts where `id >= next_callback_id` [1](#0-0) 

**`purge_shares_of_processed_requests`** (line 180): applies the identical filter to the unvalidated section [2](#0-1) 

**`insert`** (line 111-114): unconditionally inserts into the unvalidated pool with no size check, no signature check, no callback ID range check [3](#0-2) 

The existing test `test_validation_of_shares_above_known_requests` explicitly confirms the blind spot — it inserts a share with `callback_id = 1` when `next_callback_id = 1` (i.e., `id >= next_callback_id`), calls `validate_shares`, and asserts `changes.is_empty()`. The artifact stays in the unvalidated pool with no action ever taken on it. [4](#0-3) 

---

### Title
Unvalidated Canister HTTP Pool Grows Unboundedly via Future-Callback-ID Artifact Flooding — (`rs/https_outcalls/consensus/src/pool_manager.rs`)

### Summary
A malicious subnet peer can insert `CanisterHttpResponseArtifact` entries with `callback_id >= next_callback_id` into the unvalidated pool. Both the validation path and the purge path apply the filter `id < next_callback_id`, so these artifacts are permanently invisible to both routines. They accumulate without bound, exhausting replica heap memory.

### Finding Description
`CanisterHttpPoolManagerImpl` has two routines that process the unvalidated pool:

1. **`validate_shares`** — filters with `.filter(|artifact| artifact.share.content.id() < next_callback_id)` before doing any work. [5](#0-4) 

2. **`purge_shares_of_processed_requests`** — applies the same guard to the unvalidated section with the comment *"Only check the unvalidated shares belonging to the requests that we can validate."* [6](#0-5) 

The `insert` path in `CanisterHttpPoolImpl` performs no admission control whatsoever — no callback ID range check, no signature verification, no pool size cap: [3](#0-2) 

`next_callback_id` is read directly from replicated state, which is publicly observable via `read_state` requests: [7](#0-6) 

Any artifact with `id >= next_callback_id` therefore enters the pool and is never touched again — not validated, not invalidated, not purged — regardless of how many consensus rounds pass or how many requests complete.

### Impact Explanation
The unvalidated pool is an in-memory `BTreeMap`-backed `PoolSection` with no capacity limit. A single malicious subnet node can insert arbitrarily many artifacts (each carrying up to ~2 MB of response payload for non-replicated requests) with callback IDs set to `[next_callback_id, next_callback_id + N]`. These entries persist across all future `generate_change_set` calls. Sustained flooding exhausts the replica's heap, causing an OOM crash and subnet stall — a complete denial of service for the affected subnet.

### Likelihood Explanation
The attacker must be an authenticated subnet node (IC P2P uses mutual TLS with node certificates), so this is not reachable from the open internet. However, a single compromised or malicious node — well below the Byzantine fault threshold — is sufficient. `next_callback_id` is trivially observable from public replicated state. No special privileges, key material, or governance majority are required beyond subnet membership.

### Recommendation
Apply one or more of the following mitigations:

1. **Reject at insertion time**: In `CanisterHttpPoolImpl::insert`, check `artifact.message.share.content.id() < next_callback_id` (or against a reasonable upper bound) and drop artifacts that fall outside the valid range before they enter the pool.
2. **Purge future-ID artifacts unconditionally**: In `purge_shares_of_processed_requests`, add a separate pass that emits `RemoveUnvalidated` for every unvalidated artifact whose `id >= next_callback_id`, rather than silently skipping them.
3. **Cap the unvalidated pool**: Enforce a hard maximum entry count (or byte budget) on `PoolSection` for the canister HTTP pool, evicting oldest/lowest-priority entries when the cap is reached.

### Proof of Concept
```rust
// next_callback_id is observable; assume it is N.
// Insert 10_000 artifacts with ids [N, N+9999] into the unvalidated pool.
for i in 0..10_000u64 {
    let share = make_share_with_callback_id(next_callback_id + i);
    pool.insert(UnvalidatedArtifact { message: share, peer_id, timestamp });
}
// Run generate_change_set repeatedly — pool size never decreases.
for _ in 0..100 {
    let changes = pool_manager.generate_change_set(&pool);
    pool.apply(changes);
    assert_eq!(pool.get_unvalidated_artifacts().count(), 10_000); // never purged
}
```
The existing test `test_validation_of_shares_above_known_requests` already demonstrates the silent-skip behavior for a single such artifact; scaling it to 10 000 entries with a memory-size assertion would constitute a complete proof. [8](#0-7)

### Citations

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L176-188)
```rust
            .chain(
                canister_http_pool
                    .get_unvalidated_artifacts()
                    // Only check the unvalidated shares belonging to the requests that we can validate.
                    .filter(|artifact| artifact.share.content.id() < next_callback_id)
                    .filter_map(|artifact| {
                        let share = &artifact.share;
                        if active_callback_ids.contains(&share.content.id()) {
                            None
                        } else {
                            Some(CanisterHttpChangeAction::RemoveUnvalidated(share.clone()))
                        }
                    }),
```

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L482-485)
```rust
        canister_http_pool
            .get_unvalidated_artifacts()
            .filter(|artifact| artifact.share.content.id() < next_callback_id)
            .filter_map(|artifact| {
```

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L679-686)
```rust
    fn next_callback_id(&self) -> CallbackId {
        self.state_reader
            .get_latest_state()
            .get_ref()
            .metadata
            .subnet_call_context_manager
            .next_callback_id()
    }
```

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L795-896)
```rust
    pub fn test_validation_of_shares_above_known_requests() {
        ic_test_utilities::artifact_pool_config::with_test_pool_config(|pool_config| {
            with_test_replica_logger(|log| {
                let Dependencies {
                    pool,
                    replica_config,
                    crypto,
                    state_manager,
                    registry,
                    ..
                } = dependencies(pool_config.clone(), 5);
                let mut shim_mock = MockNonBlockingChannel::<CanisterHttpRequest>::new();
                shim_mock
                    .expect_try_receive()
                    .return_const(Err(TryReceiveError::Empty));

                let request = test_request_context(
                    Replication::FullyReplicated,
                    PricingVersion::Legacy,
                    None,
                );

                state_manager
                    .get_mut()
                    .expect_get_latest_state()
                    .return_const(Labeled::new(
                        Height::from(1),
                        Arc::new(state_with_pending_http_calls(BTreeMap::from([(
                            CallbackId::from(0),
                            request,
                        )]))),
                    ));

                let mut canister_http_pool =
                    CanisterHttpPoolImpl::new(MetricsRegistry::new(), no_op_logger());

                // Try to insert a share for request id 1 (while the next expected one is the
                // default value 0).
                {
                    let response_metadata = CanisterHttpResponseReceipt {
                        metadata: CanisterHttpResponseMetadata {
                            id: CallbackId::from(1),
                            registry_version: RegistryVersion::from(1),
                            content_hash: CryptoHashOf::new(CryptoHash(vec![])),
                            content_size: 0,
                            is_reject: false,
                            replica_version: ReplicaVersion::default(),
                        },
                        payment_receipt: CanisterHttpPaymentReceipt::default(),
                    };

                    let signature = crypto
                        .sign(
                            &response_metadata,
                            replica_config.node_id,
                            RegistryVersion::from(1),
                        )
                        .unwrap();

                    let share = Signed {
                        content: response_metadata.clone(),
                        signature,
                    };

                    let artifact = CanisterHttpResponseArtifact {
                        share,
                        response: None,
                    };

                    canister_http_pool.insert(UnvalidatedArtifact {
                        message: artifact,
                        peer_id: replica_config.node_id,
                        timestamp: UNIX_EPOCH,
                    });
                }

                let shim: Arc<Mutex<CanisterHttpAdapterClient>> =
                    Arc::new(Mutex::new(Box::new(shim_mock)));

                let pool_manager = CanisterHttpPoolManagerImpl::new(
                    state_manager as Arc<_>,
                    shim,
                    crypto,
                    pool.get_cache(),
                    replica_config,
                    SubnetType::Application,
                    Arc::clone(&registry) as Arc<_>,
                    MetricsRegistry::new(),
                    log,
                );

                let changes = pool_manager.validate_shares(
                    pool.get_cache().as_ref(),
                    &canister_http_pool,
                    Height::from(0),
                );

                // Make sure the changes are empty (share was filtered out)
                assert!(changes.is_empty());
            })
        });
    }
```

**File:** rs/artifact_pool/src/canister_http_pool.rs (L111-114)
```rust
    fn insert(&mut self, artifact: UnvalidatedArtifact<CanisterHttpResponseArtifact>) {
        let id = artifact.message.id();
        self.unvalidated.insert(id, artifact.message);
    }
```
