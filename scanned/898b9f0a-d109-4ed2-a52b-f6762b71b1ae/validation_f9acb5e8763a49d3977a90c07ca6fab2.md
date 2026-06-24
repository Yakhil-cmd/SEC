### Title
Unbounded Unvalidated DKG Pool Growth via Deferred Remote Dealings — (`rs/consensus/dkg/src/lib.rs`, `rs/replica/setup_ic_network/src/lib.rs`)

---

### Summary

A single Byzantine subnet node (P2P peer) can flood the unvalidated DKG pool with crafted messages carrying remote `NiDkgTargetSubnet::Remote` DKG IDs that are absent from the current summary configs. Because `validate_dealings_for_dealer` silently defers these messages indefinitely, the P2P slot table for DKG has no per-peer limit (`SLOT_TABLE_NO_LIMIT`), and `DkgPoolImpl` has no unvalidated-section size cap, the pool grows without bound until the DKG interval advances — potentially causing OOM on the victim replica.

---

### Finding Description

**Step 1 — Bouncer admits all messages at `start_height`.**

`DkgBouncer::new_bouncer` returns `BouncerValue::Wants` for any `DkgMessageId` whose `height == start_height`, with no inspection of the DKG ID content: [1](#0-0) 

**Step 2 — P2P slot table has no per-peer limit for DKG.**

The DKG channel is registered with `SLOT_TABLE_NO_LIMIT = usize::MAX`: [2](#0-1) [3](#0-2) 

The receiver enforces the limit as `peer_slot_table_len < self.slot_limit`, which is always true when `slot_limit = usize::MAX`: [4](#0-3) 

Contrast with ingress, which uses `SLOT_TABLE_LIMIT_INGRESS = 50_000` precisely to prevent this class of attack.

**Step 3 — `DkgPoolImpl` has no unvalidated-section size cap.**

`DkgPoolImpl::insert` inserts unconditionally into a `PoolSection` (a `BTreeMap`) with no quota, no per-peer counter, and no maximum size: [5](#0-4) 

**Step 4 — `validate_dealings_for_dealer` silently defers remote dealings with unknown DKG IDs.**

When a dealing's `target_subnet` is `Remote` and the DKG ID is not in the current configs, the function returns `Mutations::new()` — no removal, no invalidation: [6](#0-5) 

The message stays in the unvalidated pool indefinitely. The only eviction path is a `Purge` triggered when `start_height` advances past the current DKG interval: [7](#0-6) 

**Step 5 — No signature or dealing verification before pool insertion.**

The `FetchArtifact` assembler fetches and deserializes the artifact bytes and forwards them to the pool without any cryptographic verification. The first cryptographic check (`crypto_validate_dealing`) is only reached after the config lookup at line 207, which is never reached for deferred remote dealings.

---

### Impact Explanation

A Byzantine subnet node can craft N distinct DKG messages (different dealing bytes → different `CryptoHash` → different `DkgMessageId`) with:
- `content.version == ReplicaVersion::default()`
- `content.dkg_id.start_block_height == dkg_start_height`
- `content.dkg_id.target_subnet = NiDkgTargetSubnet::Remote(<arbitrary_target_id>)`

It advertises each on a distinct P2P slot number. All are admitted (no slot limit), fetched, inserted into the unvalidated pool, and permanently deferred by `validate_dealings_for_dealer`. The pool grows at the rate the attacker can send messages, bounded only by the DKG interval length. A DKG interval can be up to 499 blocks (~8 minutes). During this window, the attacker can exhaust replica memory, causing OOM crash or severe performance degradation on the targeted replica.

---

### Likelihood Explanation

- **Attacker capability:** A single Byzantine subnet node — below the `f < n/3` fault threshold. No key compromise, no governance majority, no DDoS required.
- **Exploit complexity:** Low. The attacker only needs to send P2P slot updates with crafted `DkgMessageId`s at the correct height. No valid crypto material is needed because the deferral branch is taken before any signature or dealing verification.
- **Natural bound:** The attack resets at each DKG interval advance (purge). However, within an interval the damage is unbounded, and the attacker can repeat across intervals.

---

### Recommendation

1. **Apply a per-peer slot table limit for DKG**, analogous to `SLOT_TABLE_LIMIT_INGRESS = 50_000` for ingress. In `rs/replica/setup_ic_network/src/lib.rs`, replace `SLOT_TABLE_NO_LIMIT` for the DKG channel with a bounded constant sized to the maximum legitimate DKG dealings per interval (number of dealers × number of DKG configs, typically O(100)).

2. **Reject (not defer) remote dealings whose DKG ID is structurally invalid** — e.g., whose `dealer` is not a member of the local subnet, detectable without a config lookup.

3. **Add a size cap to `DkgPoolImpl`'s unvalidated section**, evicting oldest entries when the cap is exceeded.

---

### Proof of Concept

```rust
// Pseudocode fuzz harness
let mut pool = DkgPoolImpl::new(MetricsRegistry::new(), logger, start_height);
for i in 0..1_000_000u64 {
    let remote_dkg_id = NiDkgId {
        start_block_height: start_height,
        dealer_subnet: subnet_test_id(0),
        dkg_tag: NiDkgTag::LowThreshold,
        target_subnet: NiDkgTargetSubnet::Remote(NiDkgTargetId::new([i as u8; 32])),
    };
    let msg = craft_dealing_with_dkg_id(remote_dkg_id); // arbitrary bytes, no valid sig needed
    pool.insert(UnvalidatedArtifact { message: msg, peer_id: attacker_node, timestamp: UNIX_EPOCH });
}
// on_state_change defers all of them:
let changes = dkg.on_state_change(&pool);
assert!(changes.is_empty()); // passes — all deferred
assert_eq!(pool.get_unvalidated().count(), 1_000_000); // pool unbounded
```

The existing test at `rs/consensus/dkg/src/lib.rs:2168-2172` already demonstrates the deferral behavior with 2 messages and explicitly asserts `pool.get_unvalidated().count() == 2` after `on_state_change` returns empty — confirming the deferral path is production behavior, not a test artifact. [8](#0-7)

### Citations

**File:** rs/consensus/dkg/src/lib.rs (L207-211)
```rust
        let config = match configs.get(message_dkg_id) {
            Some(config) => config,
            None if message_dkg_id.target_subnet.is_remote() => {
                return Mutations::new();
            }
```

**File:** rs/consensus/dkg/src/lib.rs (L302-304)
```rust
        if start_height > dkg_pool.get_current_start_height() {
            return ChangeAction::Purge(start_height).into();
        }
```

**File:** rs/consensus/dkg/src/lib.rs (L396-402)
```rust
        Box::new(move |id| {
            use std::cmp::Ordering;
            match id.height.cmp(&start_height) {
                Ordering::Equal => BouncerValue::Wants,
                Ordering::Greater => BouncerValue::MaybeWantsLater,
                Ordering::Less => BouncerValue::Unwanted,
            }
```

**File:** rs/consensus/dkg/src/lib.rs (L2168-2172)
```rust
                assert!(
                    receiver_dkg.on_state_change(&dkg_pool).is_empty(),
                    "dealing should be deferred while context is missing",
                );
                assert_eq!(dkg_pool.get_unvalidated().count(), 2);
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

**File:** rs/p2p/consensus_manager/src/receiver.rs (L372-380)
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
```

**File:** rs/artifact_pool/src/dkg_pool.rs (L89-92)
```rust
    fn insert(&mut self, artifact: UnvalidatedArtifact<consensus::dkg::Message>) {
        self.unvalidated
            .insert(DkgMessageId::from(&artifact.message), artifact);
    }
```
