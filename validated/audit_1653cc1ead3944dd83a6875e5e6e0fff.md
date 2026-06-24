Audit Report

## Title
Unbounded Unvalidated DKG Pool Growth via Deferred Remote Dealings — (`rs/consensus/dkg/src/lib.rs`, `rs/replica/setup_ic_network/src/lib.rs`)

## Summary
A single Byzantine subnet node can flood the unvalidated DKG pool by advertising crafted `NiDkgTargetSubnet::Remote` dealings whose DKG IDs are absent from the current summary configs. Because the DKG P2P channel is registered with `SLOT_TABLE_NO_LIMIT`, `validate_dealings_for_dealer` silently defers these messages indefinitely, and `DkgPoolImpl::insert` has no size cap, the pool grows without bound until the DKG interval advances — potentially causing OOM crash or severe performance degradation on the targeted replica.

## Finding Description

**Step 1 — Bouncer admits all messages at `start_height` without inspecting DKG ID content.**

`DkgBouncer::new_bouncer` returns `BouncerValue::Wants` for any `DkgMessageId` whose `height == start_height`, regardless of the DKG ID's `target_subnet` field: [1](#0-0) 

**Step 2 — DKG channel is registered with no per-peer slot limit.**

In `rs/replica/setup_ic_network/src/lib.rs`, the DKG assembler is registered with `SLOT_TABLE_NO_LIMIT = usize::MAX`: [2](#0-1) [3](#0-2) 

The receiver's guard `peer_slot_table_len < self.slot_limit` is trivially satisfied when `slot_limit = usize::MAX`, so every advertised slot is accepted: [4](#0-3) 

Contrast with ingress, which uses `SLOT_TABLE_LIMIT_INGRESS = 50_000` precisely to prevent this class of attack.

**Step 3 — `DkgPoolImpl::insert` has no size cap.**

Insertion into the unvalidated section is unconditional — no quota, no per-peer counter, no maximum size: [5](#0-4) 

**Step 4 — `validate_dealings_for_dealer` silently defers remote dealings with unknown DKG IDs.**

When `target_subnet` is `Remote` and the DKG ID is absent from the current configs, the function returns `Mutations::new()` — no removal, no invalidation, no error: [6](#0-5) 

The message remains in the unvalidated pool indefinitely. The only eviction path is a `Purge` triggered when `start_height` advances past the current DKG interval: [7](#0-6) 

**Step 5 — No cryptographic verification before pool insertion.**

The `FetchArtifact` assembler inserts the deserialized artifact directly into the pool. The first cryptographic check (`crypto_validate_dealing`) is only reached after the config lookup at line 207, which is never reached for deferred remote dealings — so no valid crypto material is required from the attacker.

**Step 6 — Existing test confirms the deferral path is production behavior.**

The test at lines 2168–2172 explicitly asserts that `on_state_change` returns empty and the pool retains 2 unvalidated messages after processing: [8](#0-7) 

## Impact Explanation

A Byzantine subnet node can craft arbitrarily many distinct DKG messages (different dealing bytes → different `CryptoHash` → different `DkgMessageId`) with `content.dkg_id.target_subnet = NiDkgTargetSubnet::Remote(<arbitrary_target_id>)` and `start_block_height == dkg_start_height`. All are admitted by the bouncer, fetched, inserted into the unvalidated pool, and permanently deferred. The pool grows at the rate the attacker can send messages, bounded only by the DKG interval length (up to 499 blocks, ~8 minutes). Within this window the attacker can exhaust replica memory, causing OOM crash or severe performance degradation.

This matches the allowed impact: **High ($2,000–$10,000) — Application/platform-level DoS, crash, consensus blocking, or subnet availability impact not based on raw volumetric DDoS.**

## Likelihood Explanation

- **Attacker capability:** A single Byzantine subnet node, below the `f < n/3` fault threshold. No key compromise, no governance majority, no DDoS required.
- **Exploit complexity:** Low. The attacker only needs to send P2P slot updates with crafted `DkgMessageId`s at the correct height. No valid cryptographic material is needed because the deferral branch is taken before any signature or dealing verification.
- **Repeatability:** The attack resets at each DKG interval advance (purge), but can be repeated across every interval indefinitely.

## Recommendation

1. **Apply a per-peer slot table limit for DKG** in `rs/replica/setup_ic_network/src/lib.rs`: replace `SLOT_TABLE_NO_LIMIT` for the DKG channel with a bounded constant sized to the maximum legitimate DKG dealings per interval (number of dealers × number of DKG configs, typically O(100)).

2. **Reject (not defer) remote dealings whose dealer is not a member of the local subnet**, detectable without a config lookup, to eliminate the deferral path for structurally invalid messages.

3. **Add a size cap to `DkgPoolImpl`'s unvalidated section** in `rs/artifact_pool/src/dkg_pool.rs`, evicting oldest entries when the cap is exceeded.

## Proof of Concept

```rust
// Pseudocode fuzz harness — no valid crypto material needed
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
let changes = dkg.on_state_change(&pool);
assert!(changes.is_empty());           // all deferred — confirmed by production test
assert_eq!(pool.get_unvalidated().count(), 1_000_000); // pool unbounded
```

The existing test at `rs/consensus/dkg/src/lib.rs:2168–2172` already confirms this deferral path is production behavior: it inserts 2 remote dealings with unknown DKG IDs, calls `on_state_change`, asserts the result is empty, and asserts the pool still contains 2 unvalidated messages. [8](#0-7)

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
