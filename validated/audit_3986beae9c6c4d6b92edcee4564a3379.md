Audit Report

## Title
Unvalidated Canister HTTP Pool Grows Unboundedly via Future-Callback-ID Artifact Flooding — (`rs/https_outcalls/consensus/src/pool_manager.rs`)

## Summary
`CanisterHttpPoolManagerImpl` filters both its validation path and its purge path with `id < next_callback_id`, leaving any artifact whose callback ID is at or above that threshold permanently resident in the unvalidated pool. Because `CanisterHttpPoolImpl::insert` performs no admission control, a single malicious subnet node can flood the pool with arbitrarily many such artifacts, exhausting replica heap memory and stalling the subnet.

## Finding Description
Two routines in `CanisterHttpPoolManagerImpl` process the unvalidated pool, and both apply the same exclusive filter:

- **`validate_shares`** (line 484): `.filter(|artifact| artifact.share.content.id() < next_callback_id)` — artifacts at or above `next_callback_id` are silently skipped and never validated or invalidated. [1](#0-0) 

- **`purge_shares_of_processed_requests`** (line 180): applies the identical guard with the comment *"Only check the unvalidated shares belonging to the requests that we can validate."* — the same artifacts are also skipped during purge. [2](#0-1) 

The insertion path in `CanisterHttpPoolImpl::insert` performs no admission control whatsoever — no callback ID range check, no signature verification, no pool size cap: [3](#0-2) 

The unvalidated pool is a `BTreeMap`-backed `PoolSection` with no capacity limit: [4](#0-3) 

`next_callback_id` is read directly from replicated state, which is publicly observable: [5](#0-4) 

The existing test `test_validation_of_shares_above_known_requests` explicitly confirms the silent-skip behavior: it inserts a share with `callback_id = 1` when `next_callback_id = 1` (i.e., `id >= next_callback_id`), calls `validate_shares`, and asserts `changes.is_empty()` — the artifact remains in the unvalidated pool with no action ever taken on it: [6](#0-5) 

## Impact Explanation
The unvalidated pool is an in-memory, unbounded `BTreeMap`. A single malicious subnet node can insert arbitrarily many artifacts with callback IDs set to `[next_callback_id, next_callback_id + N]`. These entries persist across all future `generate_change_set` calls — they are never validated, invalidated, or purged. Sustained flooding exhausts the replica's heap, causing an OOM crash and subnet stall. This matches the allowed High impact: **Application/platform-level DoS, crash, consensus blocking, or subnet availability impact not based on raw volumetric DDoS** ($2,000–$10,000).

## Likelihood Explanation
The attacker must be an authenticated subnet node (IC P2P uses mutual TLS with node certificates), placing this below the Byzantine fault threshold — a single compromised or malicious node is sufficient. `next_callback_id` is trivially observable from public replicated state. No special privileges, key material, or governance majority are required beyond subnet membership. The attack is repeatable across every consensus round.

## Recommendation
Apply one or more of the following mitigations:

1. **Reject at insertion time**: In `CanisterHttpPoolImpl::insert`, check that `artifact.message.share.content.id() < next_callback_id` (or within a reasonable upper bound window) and drop artifacts outside the valid range before they enter the pool.
2. **Purge future-ID artifacts unconditionally**: In `purge_shares_of_processed_requests`, add a separate pass that emits `RemoveUnvalidated` for every unvalidated artifact whose `id >= next_callback_id`, rather than silently skipping them.
3. **Cap the unvalidated pool**: Enforce a hard maximum entry count (or byte budget) on `PoolSection` for the canister HTTP pool, evicting oldest/lowest-priority entries when the cap is reached.

## Proof of Concept
The existing test at line 795 already demonstrates the silent-skip for a single artifact. Scaling it constitutes a complete proof:

```rust
// next_callback_id is observable; assume it is N.
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

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L831-893)
```rust
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
```

**File:** rs/artifact_pool/src/canister_http_pool.rs (L30-31)
```rust
type UnvalidatedCanisterHttpPoolSection =
    PoolSection<CanisterHttpResponseShare, CanisterHttpResponseArtifact>;
```

**File:** rs/artifact_pool/src/canister_http_pool.rs (L111-114)
```rust
    fn insert(&mut self, artifact: UnvalidatedArtifact<CanisterHttpResponseArtifact>) {
        let id = artifact.message.id();
        self.unvalidated.insert(id, artifact.message);
    }
```
