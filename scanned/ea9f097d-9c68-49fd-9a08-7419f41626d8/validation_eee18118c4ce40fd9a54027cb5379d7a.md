### Title
Unbounded Unvalidated Pool Accumulation via Deferred Remote DKG Dealings — (`rs/consensus/dkg/src/lib.rs`)

---

### Summary

The `validate_dealings_for_dealer` function silently defers remote DKG dealings whose `NiDkgId` has no matching config, returning `Mutations::new()` without removing the artifact. Combined with the DKG channel being registered with `SLOT_TABLE_NO_LIMIT`, a single Byzantine subnet member can flood the unvalidated pool with arbitrarily many distinct remote DKG dealings within one DKG interval, causing unbounded memory growth on honest replicas.

---

### Finding Description

In `validate_dealings_for_dealer`, when a dealing's `dkg_id` is not found in the current configs map and `target_subnet.is_remote()` is true, the function returns `Mutations::new()` — no `HandleInvalid`, no `RemoveFromUnvalidated`: [1](#0-0) 

The dealing remains in the unvalidated pool indefinitely until the next DKG interval purge. The purge only fires when `start_height > dkg_pool.get_current_start_height()`: [2](#0-1) 

The `DkgPoolImpl::insert()` has no size guard — it inserts unconditionally into a `PoolSection` (a `BTreeMap`): [3](#0-2) 

The `DkgBouncer` only filters by height, accepting any dealing at the current start height regardless of `target_subnet` type: [4](#0-3) 

Critically, the DKG artifact channel is registered with `SLOT_TABLE_NO_LIMIT = usize::MAX`, meaning there is no per-peer cap on how many distinct DKG artifact IDs a peer can advertise: [5](#0-4) [6](#0-5) 

The `DkgMessageId` is keyed by `crypto_hash(message)`, so each message with a distinct `NiDkgTargetId` (a 32-byte field with 2^256 possible values) or distinct `NiDkgDealing` bytes produces a unique pool entry: [7](#0-6) 

The `NiDkgTargetId` is simply a 32-byte array with no structural constraints: [8](#0-7) 

The existing test `test_remote_dealing_validation_is_deferred_until_context_exists` explicitly confirms that deferred remote dealings remain in the unvalidated pool and are only cleared at the next DKG interval: [9](#0-8) 

---

### Impact Explanation

A Byzantine subnet member (single node, below fault threshold) can:

1. Craft N distinct DKG messages with `target_subnet = NiDkgTargetSubnet::Remote(distinct_id_i)` for i=1..N, each with the current `start_block_height`.
2. Advertise all N artifacts via the P2P slot table (no limit for DKG).
3. Each message passes the `DkgBouncer` height check and is inserted into the unvalidated pool.
4. `validate_dealings_for_dealer` returns `Mutations::new()` for each — no removal occurs.
5. The pool grows without bound until the next DKG interval purge (~500 blocks, ~500 seconds at 1s block time).

Each `NiDkgDealing` contains `NiDkgCspDealing` (BLS12-381 crypto material, several KB each). Injecting millions of such messages within one DKG interval can exhaust heap memory on honest replicas, causing OOM kills and liveness failure of the DKG pipeline and subnet stall.

---

### Likelihood Explanation

- **Attacker prerequisite**: Must be a legitimate subnet member (Byzantine node below fault threshold). IC P2P uses mutual TLS with node certificates, so only registered subnet members can connect. This is a meaningful but not extreme barrier — a single compromised or malicious node suffices.
- **Ease of exploit**: Crafting distinct remote DKG messages is trivial (vary the 32-byte `NiDkgTargetId`). No valid crypto material is required since signature and dealing verification are never reached for the deferred path.
- **No rate limiting**: `SLOT_TABLE_NO_LIMIT` for DKG means no per-peer cap exists.
- **Bounded window**: The pool is purged at each DKG interval, so the attack must be sustained within ~500 blocks. This is a partial mitigation but does not prevent within-interval exhaustion.

---

### Recommendation

1. **Add a pool size cap** in `DkgPoolImpl::insert()` or in `on_state_change` before processing unvalidated dealings, rejecting new entries once a configurable limit is reached.
2. **Apply a slot table limit for DKG** analogous to `SLOT_TABLE_LIMIT_INGRESS` (50,000) rather than `SLOT_TABLE_NO_LIMIT`.
3. **Reject deferred remote dealings after a configurable count** per `(dealer_id, start_height)` tuple, or track the number of distinct unknown remote target IDs and cap them.
4. **Consider issuing `HandleInvalid`** for remote dealings whose `target_subnet` remote ID is not plausibly associated with any pending `SetupInitialDKG` or `ReshareChainKey` context visible in the certified state, rather than deferring unconditionally.

---

### Proof of Concept

```rust
// Byzantine subnet member crafts N distinct remote DKG dealings
let start_height = dkg_pool.get_current_start_height();
for i in 0u64..1_000_000 {
    let target_id = NiDkgTargetId::new(i.to_le_bytes().try_into().unwrap()); // distinct IDs
    let dkg_id = NiDkgId {
        start_block_height: start_height,
        dealer_subnet: attacker_subnet_id,
        dkg_tag: NiDkgTag::LowThreshold,
        target_subnet: NiDkgTargetSubnet::Remote(target_id), // unknown config
    };
    let msg = craft_signed_dealing(attacker_node_key, dkg_id); // valid signature, arbitrary dealing bytes
    dkg_pool.insert(UnvalidatedArtifact { message: msg, peer_id: attacker_node_id, timestamp: now });
}

// Call validate repeatedly — each call returns Mutations::new(), nothing is removed
for _ in 0..100 {
    let changeset = dkg_impl.on_state_change(&dkg_pool);
    assert!(changeset.iter().all(|a| !matches!(a, ChangeAction::HandleInvalid(..))));
    // Pool size grows without bound
}
assert!(dkg_pool.get_unvalidated().count() == 1_000_000); // unbounded accumulation
```

### Citations

**File:** rs/consensus/dkg/src/lib.rs (L207-218)
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

**File:** rs/consensus/dkg/src/lib.rs (L2168-2209)
```rust
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

**File:** rs/types/types/src/consensus/dkg.rs (L42-55)
```rust
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Hash, Debug, Deserialize, Serialize)]
pub struct DkgMessageId {
    pub hash: CryptoHashOf<Message>,
    pub height: Height,
}

impl From<&Message> for DkgMessageId {
    fn from(msg: &Message) -> Self {
        Self {
            hash: crypto_hash(msg),
            height: msg.content.dkg_id.start_block_height,
        }
    }
}
```

**File:** rs/types/types/src/crypto/threshold_sig/ni_dkg.rs (L169-183)
```rust
#[derive(Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Hash, Debug)]
#[cfg_attr(test, derive(ExhaustiveSet))]
pub struct NiDkgTargetId([u8; NiDkgTargetId::SIZE]);
ic_crypto_internal_types::derive_serde!(NiDkgTargetId, NiDkgTargetId::SIZE);

impl NiDkgTargetId {
    pub const SIZE: usize = 32;

    pub const fn new(id: [u8; NiDkgTargetId::SIZE]) -> Self {
        NiDkgTargetId(id)
    }

    pub fn to_vec(&self) -> Vec<u8> {
        self.0.to_vec()
    }
```
