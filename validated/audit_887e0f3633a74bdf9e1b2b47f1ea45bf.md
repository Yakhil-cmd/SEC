All four cited code locations match the actual source exactly. Let me verify the P2P pre-insertion path to confirm no signature check blocks crafted shares.

Audit Report

## Title
Crafted CatchUpPackageShare Height Anchors Permanently Suppress Unvalidated Pool Purger Leading to Memory Exhaustion DoS - (File: `rs/consensus/src/consensus/purger.rs`)

## Summary
The `purge_unvalidated_pool_by_expected_batch_height` function in `rs/consensus/src/consensus/purger.rs` uses a `height_range()` span check to skip purging when unprocessed CatchUpPackageShares appear needed. A Byzantine peer below the fault threshold can insert two crafted shares — one at height 0 and one at height `u64::MAX` — to permanently set the pool's height range to `[0, u64::MAX]`, causing the guard to evaluate true on every invocation and suppressing all unvalidated pool purging indefinitely. The attacker can then flood the pool with additional crafted shares that are never validated and never purged, exhausting node memory and halting subnet consensus.

## Finding Description

**Root cause 1 — Purger guard uses range span, not membership (`purger.rs` lines 198–218):**

The helper functions `below_range_max` and `above_range_min` check only the pool's aggregate min/max heights, not whether any share actually falls strictly between `catch_up_height` and `expected_batch_height`:

```rust
fn below_range_max(h: Height, range: &Option<HeightRange>) -> bool {
    range.as_ref().map(|r| h < r.max).unwrap_or(false)
}
fn above_range_min(h: Height, range: &Option<HeightRange>) -> bool {
    range.as_ref().map(|r| h > r.min).unwrap_or(false)
}
// ...
if below_range_max(catch_up_height, &unvalidated_catch_up_share_range)
    && above_range_min(expected_batch_height, &unvalidated_catch_up_share_range)
{
    return;
}
```

If `range.min = 0` and `range.max = u64::MAX`, then `catch_up_height < u64::MAX` is always true and `expected_batch_height > 0` is always true (batch height starts at 1), so the purger permanently returns early without emitting `PurgeUnvalidatedBelow`.

**Root cause 2 — `height_range()` reflects actual pool min/max (`inmemory_pool.rs` lines 152–161):**

```rust
fn height_range(&self) -> Option<HeightRange> {
    let heights = ...heights()...collect::<Vec<_>>();
    match (heights.first(), heights.last()) {
        (Some(min), Some(max)) => Some(HeightRange::new(*min, *max)),
        _ => None,
    }
}
```

Inserting one share at height 0 and one at height `u64::MAX` directly sets `range.min = 0` and `range.max = u64::MAX`.

**Root cause 3 — Validator range excludes height-0 shares (`validator.rs` lines 1601–1617):**

```rust
let range = HeightRange::new(catch_up_height.increment(), max_height);
let shares = pool_reader.pool().unvalidated()
    .catch_up_package_share()
    .get_by_height_range(range);
```

A share at height 0 is below `catch_up_height.increment()` and is never processed by the validator. It stays in the unvalidated pool indefinitely, permanently anchoring `range.min = 0`.

**Root cause 4 — `u64::MAX` share produces transient error, stays in pool (`validator.rs` lines 1649–1658 and 1679–1682):**

```rust
let block = pool_reader
    .get_finalized_block(height)
    .ok_or(ValidationFailure::FinalizedBlockNotFound(height))?;
// ...
Err(ValidationError::ValidationFailed(err)) => {
    // ...warn...
    None  // share stays in unvalidated pool
}
```

`get_finalized_block(u64::MAX)` returns `None`, producing `ValidationFailure::FinalizedBlockNotFound`, which is a transient error. The share is never removed.

**Root cause 5 — `check_integrity` does not validate height bounds (`consensus.rs` lines 1737–1741):**

```rust
fn check_integrity(&self) -> bool {
    let content = &self.content;
    let random_beacon_hash = content.random_beacon.get_hash();
    &crypto_hash(content.random_beacon.as_ref()) == random_beacon_hash
}
```

Only the self-consistency of the random beacon hash is checked. Any height value passes.

**Root cause 6 — No pre-insertion signature check at P2P layer (`consensus_pool.rs` lines 700–705, `receiver.rs` lines 484–489):**

`ConsensusPoolImpl::insert` places the artifact directly into the unvalidated pool without any signature or height validation. The P2P receiver (`process_slot_update`) sends assembled artifacts straight to the pool via `UnvalidatedArtifactMutation::Insert`. Signature verification only occurs inside the validator after `validate_catch_up_share_content` succeeds — which it never does for the crafted shares.

**Deadlock:** The height-0 share cannot be purged because `PurgeUnvalidatedBelow(h)` is only emitted when the purger runs, and the purger is suppressed because the height-0 share is in the pool. The `prev_expected_batch_height` at line 219 is never updated, so the outer condition at line 193 remains satisfiable on every future invocation, but the inner guard at lines 214–218 always fires first.

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

Once the two anchor shares are in the pool, the purger emits no `PurgeUnvalidatedBelow` actions for any artifact type. The attacker continuously re-gossips height-0 shares (which are accepted by P2P, inserted into the unvalidated pool, never validated, and never purged). All honest nodes on the subnet experience unbounded unvalidated pool growth, leading to memory exhaustion, process crash, and subnet consensus halt. The attack affects all honest nodes simultaneously.

## Likelihood Explanation

A single Byzantine consensus peer below the fault threshold can execute this attack. No threshold key material, admin access, or majority corruption is required. The attacker only needs to:
1. Craft two `CatchUpPackageShare` protobuf messages with internally consistent random beacon hashes (no valid threshold signature needed for unvalidated pool insertion).
2. Broadcast them via the standard P2P gossip protocol.

The attack is persistent: the unvalidated pool is volatile (in-memory), so after a node restart the attacker immediately re-gossips the anchor shares. The attack is cheap to sustain — only two anchor messages are needed to suppress the purger, and subsequent flooding is low-cost.

## Recommendation

**Short term:** Replace the range-span check in `purge_unvalidated_pool_by_expected_batch_height` with a check that queries whether any share exists with height strictly in the interval `(catch_up_height, expected_batch_height)`, rather than checking whether the pool's aggregate `[min, max]` span covers that interval. For example:

```rust
let has_needed_share = unvalidated_pool
    .catch_up_package_share()
    .get_by_height_range(HeightRange::new(
        catch_up_height.increment(),
        expected_batch_height.decrement(),
    ))
    .next()
    .is_some();
if has_needed_share { return; }
```

**Long term:** Validate the height of all incoming `CatchUpPackageShare` artifacts at ingress (before inserting into the unvalidated pool), rejecting any share whose height is not within a reasonable window of the current finalized height. Additionally, add a bounded-age eviction mechanism for unvalidated artifacts that have been in the pool longer than a configurable threshold.

## Proof of Concept

**Step 1:** Craft share `S` with `random_beacon.height = 0`:
- Construct a `RandomBeacon` with `height = Height::from(0)` and any parent hash.
- Compute `random_beacon_hash = crypto_hash(&random_beacon)`.
- Build `CatchUpShareContent` embedding this beacon. `check_integrity()` passes since `crypto_hash(random_beacon) == random_beacon_hash`.
- Broadcast `S` to honest nodes via P2P. It is inserted into the unvalidated pool without signature verification.
- The validator's range `[catch_up_height.increment(), max_height]` excludes height 0. `S` is never processed and stays in the pool, anchoring `range.min = 0`.

**Step 2:** Craft share `S'` with `random_beacon.height = u64::MAX`:
- Same construction. `check_integrity()` passes.
- Broadcast `S'`. It is inserted into the unvalidated pool.
- The validator processes `S'` (it is within range), calls `get_finalized_block(u64::MAX)` → `None` → `ValidationFailure::FinalizedBlockNotFound` → transient error → `S'` stays in pool, anchoring `range.max = u64::MAX`.

**Step 3:** After both shares are inserted, `unvalidated_pool.catch_up_package_share().height_range()` returns `Some(HeightRange { min: Height(0), max: Height(u64::MAX) })`.

**Step 4:** On every purger invocation:
- `below_range_max(catch_up_height, &range)` → `catch_up_height < u64::MAX` → `true`
- `above_range_min(expected_batch_height, &range)` → `expected_batch_height > 0` → `true`
- `return;` — no `PurgeUnvalidatedBelow` action is emitted, `prev_expected_batch_height` is never updated.

**Step 5:** Flood the network with additional height-0 `CatchUpPackageShare` messages. Each is accepted into the unvalidated pool, never validated, never purged. Memory grows without bound until node crash.

**Minimal unit test plan:** In a test using `TestConsensusPool`, insert a crafted `CatchUpPackageShare` with height 0 and one with height `u64::MAX` into the unvalidated pool. Invoke `purge_unvalidated_pool_by_expected_batch_height` with a mock `MessageRouting` returning `expected_batch_height = Height(5)` and a finalized height of `Height(5)`. Assert that the returned `Mutations` changeset contains no `PurgeUnvalidatedBelow` action. Then insert 1000 additional height-0 shares and assert the unvalidated pool size grows unboundedly.