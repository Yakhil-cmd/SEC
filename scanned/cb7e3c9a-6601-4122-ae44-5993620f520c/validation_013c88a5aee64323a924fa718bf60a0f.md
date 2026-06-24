The code confirms the vulnerability claim. Let me trace through all the relevant components precisely.

**Bouncer window** (`rs/https_outcalls/consensus/src/gossip.rs`): [1](#0-0) [2](#0-1) 

`MAX_NUMBER_OF_REQUESTS_AHEAD = 345`. The bouncer returns `Wants` for any artifact whose `callback_id` is in `[next_callback_id, next_callback_id + 345]`, gated only by a registry version check. No signature verification occurs here.

**Purge gap** (`rs/https_outcalls/consensus/src/pool_manager.rs`): [3](#0-2) 

`purge_shares_of_processed_requests` explicitly filters unvalidated artifacts to only those with `id < next_callback_id`. Artifacts in the bouncer window (`id >= next_callback_id`) are skipped entirely.

**Validation gap** (`rs/https_outcalls/consensus/src/pool_manager.rs`): [4](#0-3) 

`validate_shares` applies the same `id < next_callback_id` filter. Artifacts in the bouncer window are never validated and never removed.

**Artifact ID structure** (`rs/types/types/src/canister_http.rs`): [5](#0-4) 

The artifact ID is the full `CanisterHttpResponseShare` (a `BasicSigned<CanisterHttpResponseReceipt>`). A Byzantine peer controlling one node key can produce an unbounded number of distinct artifact IDs for the same `callback_id` by varying `content_hash` in the metadata and signing each one — all pass the bouncer (which only checks `callback_id` and `registry_version`), all enter the unvalidated pool, and none are ever evicted.

---

### Title
Unvalidated pool unbounded growth via bouncer-window artifacts never purged or validated — (`rs/https_outcalls/consensus/src/gossip.rs`, `rs/https_outcalls/consensus/src/pool_manager.rs`)

### Summary
The canister HTTP outcalls subsystem has a structural gap between what the gossip bouncer admits into the unvalidated pool and what the pool manager ever removes. Artifacts with `callback_id` in `[next_callback_id, next_callback_id + 345]` are admitted by the bouncer but are permanently skipped by both `purge_shares_of_processed_requests` and `validate_shares`. A single Byzantine P2P peer can exploit this to grow the unvalidated pool without bound.

### Finding Description
`CanisterHttpGossipImpl::new_bouncer` returns `BouncerValue::Wants` for any artifact whose `callback_id` satisfies `next_callback_id <= id <= next_callback_id + MAX_NUMBER_OF_REQUESTS_AHEAD` (345), subject only to a registry-version equality check. No signature verification is performed at this stage.

Both pool-management functions that could remove such artifacts apply a hard `id < next_callback_id` pre-filter:

- `purge_shares_of_processed_requests` (line 180): only considers unvalidated artifacts with `id < next_callback_id` for removal.
- `validate_shares` (line 484): only considers unvalidated artifacts with `id < next_callback_id` for validation or invalidation.

Artifacts with `id >= next_callback_id` are therefore invisible to both functions and accumulate indefinitely in the unvalidated pool until `next_callback_id` advances past them.

Because the artifact ID is the full `CanisterHttpResponseShare` (including `content_hash`), a Byzantine peer holding one valid node key can produce an unlimited number of distinct artifact IDs for each `callback_id` in the window by varying `content_hash` and signing each variant. Each distinct ID is treated as a separate pool entry. The pool has no per-peer or total-size cap on the unvalidated section.

### Impact Explanation
A single Byzantine replica (below the fault threshold, no key compromise required) can cause unbounded heap growth in the unvalidated pool of any honest replica it is peered with, leading to OOM and replica crash. This affects only the targeted replica (single-replica impact), not subnet liveness directly, but a crashed replica falls behind and must resync, degrading subnet resilience.

### Likelihood Explanation
The attacker needs only one subnet membership slot and the ability to gossip crafted artifacts — both are within the standard Byzantine fault model. The registry-version check is the only bouncer gate, and the current registry version is publicly observable. The attack is repeatable every gossip round and requires no coordination with other nodes.

### Recommendation
Add an explicit eviction path for unvalidated artifacts whose `callback_id` falls in the bouncer window but is not in `active_callback_ids`. Concretely, in `purge_shares_of_processed_requests`, remove the `id < next_callback_id` pre-filter on the unvalidated pool scan, or add a second pass that evicts unvalidated artifacts with `id >= next_callback_id` that are not in `active_callback_ids`. Alternatively, enforce a hard cap on the unvalidated pool size (e.g., `subnet_size * MAX_NUMBER_OF_REQUESTS_AHEAD`) and drop excess artifacts by age or peer.

### Proof of Concept
State-machine test sketch:
1. Set `next_callback_id = K`, `active_callback_ids = {K}`.
2. For each round `r` in `1..N`, insert `M` artifacts with distinct `content_hash` values and `callback_id = K + 1` (in the bouncer window) from a single Byzantine peer.
3. Call `on_state_change` each round.
4. Assert that `canister_http_pool.get_unvalidated_artifacts().count()` grows by `M` each round and is never reduced.
5. Observe pool size = `N * M` after `N` rounds, confirming unbounded growth.

### Citations

**File:** rs/https_outcalls/consensus/src/gossip.rs (L19-19)
```rust
const MAX_NUMBER_OF_REQUESTS_AHEAD: u64 = 3 * (100 + 15);
```

**File:** rs/https_outcalls/consensus/src/gossip.rs (L77-95)
```rust
            let highest_accepted_request_id =
                CallbackId::from(next_callback_id.get() + MAX_NUMBER_OF_REQUESTS_AHEAD);

            // The https outcalls share should be fetched in two cases:
            //  - The Id of the share is part of the state which means it is active.
            //  - The callback Id is higher than the next callback Id (the next callback Id is the Id used next in execution round), but
            //    not higher that `MAX_NUMBER_OF_REQUESTS_AHEAD`.
            //    Receiving an callback Id higher is possible because the priority fn is updated periodically (every 3s) with the latest state
            //    and can therefore store stale `known_request_ids` and stale `next_callback_id`.
            if known_request_ids.contains(&id.content.id())
                || (id.content.id() >= next_callback_id
                    && id.content.id() <= highest_accepted_request_id)
            {
                BouncerValue::Wants
            } else if id.content.id() > highest_accepted_request_id {
                BouncerValue::MaybeWantsLater
            } else {
                BouncerValue::Unwanted
            }
```

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

**File:** rs/types/types/src/canister_http.rs (L1183-1195)
```rust
pub struct CanisterHttpResponseArtifact {
    pub share: CanisterHttpResponseShare,
    // The response should not be included in the case of fully replicated outcalls.
    pub response: Option<CanisterHttpResponse>,
}

impl IdentifiableArtifact for CanisterHttpResponseArtifact {
    const NAME: &'static str = "canisterhttp";
    type Id = CanisterHttpResponseId;
    fn id(&self) -> Self::Id {
        self.share.clone()
    }
}
```
