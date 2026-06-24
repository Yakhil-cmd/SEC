### Title
Unbounded Unvalidated-Pool Growth via Bouncer-Window Artifacts — (`rs/https_outcalls/consensus/src/gossip.rs`, `rs/https_outcalls/consensus/src/pool_manager.rs`)

---

### Summary

A Byzantine P2P peer can flood the unvalidated canister-HTTP artifact pool on a single replica with well-formed artifacts whose `callback_id` falls in the gossip-bouncer window `[next_callback_id, next_callback_id + 345]`. Both the purge and validate routines unconditionally skip artifacts in this window, and the pool has no per-peer or global size cap. The artifacts accumulate indefinitely, bounded only by how fast `next_callback_id` advances, which the attacker can outpace by varying `content_hash` to generate unboundedly many distinct artifact IDs per callback slot.

---

### Finding Description

**Step 1 — Bouncer admits the window.**

`CanisterHttpGossipImpl::new_bouncer` returns `BouncerValue::Wants` for any artifact whose `callback_id` satisfies:

```
id >= next_callback_id  &&  id <= next_callback_id + MAX_NUMBER_OF_REQUESTS_AHEAD
```

where `MAX_NUMBER_OF_REQUESTS_AHEAD = 3 * (100 + 15) = 345`. [1](#0-0) [2](#0-1) 

The only other check is that `registry_version` matches the current finalized version. No signer validity, no signature verification, no content-hash check is performed at this layer.

**Step 2 — Artifacts enter the pool without a size guard.**

`CanisterHttpPoolImpl::insert` unconditionally inserts into an unbounded `PoolSection` map. There is no `exceeds_limit` method, no per-peer byte/count quota, and no global pool-size cap — unlike the ingress pool which has explicit per-peer limits. [3](#0-2) 

**Step 3 — Purge skips the window.**

`purge_shares_of_processed_requests` filters unvalidated artifacts to only those with `id < next_callback_id`. Artifacts with `id >= next_callback_id` are never touched. [4](#0-3) 

**Step 4 — Validate skips the window.**

`validate_shares` applies the identical filter. Artifacts in the bouncer window are never validated, never moved, and never removed. [5](#0-4) 

**Step 5 — Unbounded distinct artifact IDs per callback slot.**

The pool key is the full `CanisterHttpResponseShare` (`BasicSigned<CanisterHttpResponseReceipt>`), which includes `content_hash` as a free field. A Byzantine peer can craft arbitrarily many distinct shares per `callback_id` by varying `content_hash`, each passing the bouncer and occupying a separate pool entry. [6](#0-5) 

---

### Impact Explanation

Each round the attacker inserts N new artifacts per callback slot across 345 slots. When `next_callback_id` advances by 1, one slot's artifacts become processable and are eventually removed, but the attacker immediately fills the new leading slot. The steady-state pool size grows as `N × 345` with N unbounded. Sustained flooding causes heap exhaustion and OOM crash on the targeted replica. The subnet continues operating (f-fault tolerance), so impact is scoped to a single replica.

---

### Likelihood Explanation

The attacker needs only a single P2P connection to the victim replica — no subnet-majority corruption, no key material, no governance access. Crafting artifacts with arbitrary `content_hash` and a matching `registry_version` requires no cryptographic capability. The bouncer refresh period is 3 seconds, giving a sustained admission rate. This is a concrete, locally testable path.

---

### Recommendation

1. **Add a pool-size cap to `CanisterHttpPoolImpl::insert`**: mirror the ingress pool's `exceeds_limit` pattern with a per-peer artifact count and/or a global unvalidated pool byte limit.
2. **Purge future-window artifacts on bouncer transition**: when the bouncer transitions an artifact from `Wants` to `Unwanted` (i.e., `callback_id < next_callback_id` and not in `active_contexts`), emit `RemoveUnvalidated` regardless of the `< next_callback_id` filter.
3. **Alternatively, remove the `< next_callback_id` guard in purge**: the guard's stated purpose is to avoid purging legitimately future artifacts, but a simple `!active_contexts.contains_key(id)` check already handles that correctly for all IDs.

---

### Proof of Concept

```
State: next_callback_id = K, subnet has http_requests enabled.

For round r = 1..R:
  Attacker sends N artifacts per callback_id in [K, K+345],
  each with a distinct content_hash (e.g., hash = [r, i, j]).
  All pass bouncer (registry_version matches, id in window).
  All inserted into unvalidated pool.
  on_state_change():
    purge_shares_of_processed_requests: skips all (id >= K)
    validate_shares: skips all (id >= K)
  Pool grows by N*345 entries per round.
  K advances by ~1 per round (normal execution).
  Net growth per round: N*345 - N ≈ N*344.

After R rounds: pool contains ~N*344*R entries.
With N=100 artifacts/slot and R=1000 rounds: ~34M entries → OOM.
```

### Citations

**File:** rs/https_outcalls/consensus/src/gossip.rs (L19-19)
```rust
const MAX_NUMBER_OF_REQUESTS_AHEAD: u64 = 3 * (100 + 15);
```

**File:** rs/https_outcalls/consensus/src/gossip.rs (L77-90)
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
```

**File:** rs/artifact_pool/src/canister_http_pool.rs (L111-114)
```rust
    fn insert(&mut self, artifact: UnvalidatedArtifact<CanisterHttpResponseArtifact>) {
        let id = artifact.message.id();
        self.unvalidated.insert(id, artifact.message);
    }
```

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L177-188)
```rust
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

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L482-484)
```rust
        canister_http_pool
            .get_unvalidated_artifacts()
            .filter(|artifact| artifact.share.content.id() < next_callback_id)
```

**File:** rs/types/types/src/canister_http.rs (L1183-1187)
```rust
pub struct CanisterHttpResponseArtifact {
    pub share: CanisterHttpResponseShare,
    // The response should not be included in the case of fully replicated outcalls.
    pub response: Option<CanisterHttpResponse>,
}
```
