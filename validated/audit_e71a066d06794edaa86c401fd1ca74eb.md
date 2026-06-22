### Title
Maliciously Crafted CatchUpPackageShare Heights Cause Permanent Unvalidated Pool Purge Bypass Leading to Memory Exhaustion - (File: `rs/consensus/src/consensus/purger.rs`)

---

### Summary

The `Purger::purge_unvalidated_pool_by_expected_batch_height` function in `rs/consensus/src/consensus/purger.rs` contains an early-return guard that checks whether the unvalidated `CatchUpPackageShare` pool's height range spans a "needed" interval. A malicious consensus peer can inject two crafted `CatchUpPackageShare` messages — one with `random_beacon.height = 0` and one with `random_beacon.height = u64::MAX` — to permanently set the pool's `height_range()` to `[0, u64::MAX]`. This causes the guard condition to evaluate to `true` on every purger invocation, permanently suppressing all unvalidated pool purging. The malicious node can then flood the unvalidated pool with height-0 shares indefinitely, exhausting node memory and causing a subnet-wide denial of service.

---

### Finding Description

**Root cause 1 — Purger early-return guard (`rs/consensus/src/consensus/purger.rs`, lines 211–218):**

```rust
// Skip purging if we have unprocessed but needed CatchUpPackageShare
let unvalidated_catch_up_share_range =
    unvalidated_pool.catch_up_package_share().height_range();
if below_range_max(catch_up_height, &unvalidated_catch_up_share_range)
    && above_range_min(expected_batch_height, &unvalidated_catch_up_share_range)
{
    return;
}
```

Where:
- `below_range_max(h, range)` → `h < range.max`
- `above_range_min(h, range)` → `h > range.min`

If the unvalidated pool contains a share with `height = 0` (setting `range.min = 0`) and a share with `height = u64::MAX` (setting `range.max = u64::MAX`), then:
- `catch_up_height < u64::MAX` → always `true`
- `expected_batch_height > 0` → always `true` (expected batch height starts at 1)

The purger returns early on every call, emitting no `PurgeUnvalidatedBelow` action.

**Root cause 2 — Validator leaves transient-error shares in the pool (`rs/consensus/src/consensus/validator.rs`, lines 1649–1658):**

```rust
Err(ValidationError::ValidationFailed(err)) => {
    if self.unvalidated_for_too_long(pool_reader, &share.get_id()) {
        warn!(...);
    }
    None  // ← share stays in unvalidated pool forever
}
```

`validate_catch_up_share_content` returns `ValidationFailure::FinalizedBlockNotFound(height)` for any height where no finalized block exists (e.g., `u64::MAX`). This is classified as a transient error, so the share is never removed.

**Root cause 3 — `check_integrity` does not validate height bounds (`rs/types/types/src/consensus.rs`, lines 1737–1741):**

```rust
fn check_integrity(&self) -> bool {
    let content = &self.content;
    let random_beacon_hash = content.random_beacon.get_hash();
    &crypto_hash(content.random_beacon.as_ref()) == random_beacon_hash
}
```

`check_integrity` for `CatchUpPackageShare` only verifies the random beacon hash is self-consistent. It does not validate that the height is within any reasonable range. An attacker can craft a `RandomBeacon` with `height = 0` or `height = u64::MAX`, compute its hash, and the integrity check passes.

**Root cause 4 — Validator range excludes height-0 shares from processing (`rs/consensus/src/consensus/validator.rs`, lines 1601–1617):**

```rust
let range = HeightRange::new(catch_up_height.increment(), max_height);
let shares = pool_reader
    .pool()
    .unvalidated()
    .catch_up_package_share()
    .get_by_height_range(range);
```

A share with `height = 0` (or any height ≤ `catch_up_height`) is excluded from the validation range and is never processed — it stays in the unvalidated pool indefinitely, permanently anchoring `range.min = 0`.

---

### Impact Explanation

Once the two anchor shares are in the unvalidated pool, the purger's `purge_unvalidated_pool_by_expected_batch_height` permanently returns early. The malicious node can then continuously broadcast additional `CatchUpPackageShare` messages with height 0 (or any height ≤ `catch_up_height`). These are accepted by the P2P layer, inserted into the unvalidated pool, never validated (below the validator's range), and never purged (purger is suppressed). The unvalidated pool grows without bound, exhausting node memory. This can take down all honest nodes in the subnet, halting consensus.

---

### Likelihood Explanation

A single malicious node below the consensus fault threshold can execute this attack. No threshold key, admin access, or majority corruption is required. The attacker only needs to:
1. Craft two `CatchUpPackageShare` messages with internally consistent hashes (trivial — no valid threshold signature is needed to insert into the unvalidated pool).
2. Broadcast them to honest nodes via the standard P2P gossip protocol.

The attack is persistent: once the two anchor shares are in the pool, the purger is permanently suppressed until a node restart (which does not help, since the shares are re-gossiped). The attack is also cheap to sustain — the attacker only needs to keep sending height-0 shares.

---

### Recommendation

**Short term:** In `purge_unvalidated_pool_by_expected_batch_height`, replace the raw `range.min`/`range.max` check with a check that only considers shares whose height falls within a bounded interval of the current finalized or catch-up height. Specifically, the guard should only skip purging if there exists a share with height strictly between `catch_up_height` and `expected_batch_height` — not if the range merely spans that interval due to extreme outlier heights.

**Long term:** Validate the height of all incoming `CatchUpPackageShare` artifacts at ingress (before inserting into the unvalidated pool), rejecting any share whose height is not within a reasonable window of the current finalized height. Additionally, consider purging unvalidated artifacts that have been in the pool longer than a configurable bound.

---

### Proof of Concept

**Step 1:** Craft share `S` with `random_beacon.height = 0`:
- Construct a `RandomBeacon` with `height = Height::from(0)` and any valid `parent` hash.
- Compute `random_beacon_hash = crypto_hash(&random_beacon)`.
- Build `CatchUpShareContent` embedding this beacon. `check_integrity()` passes since `crypto_hash(random_beacon) == random_beacon_hash`.
- `S.height() = 0 ≤ catch_up_height`, so the validator's range `[catch_up_height.increment(), max_height]` excludes it. `S` is never processed and stays in the unvalidated pool.

**Step 2:** Craft share `S'` with `random_beacon.height = u64::MAX`:
- Construct a `RandomBeacon` with `height = Height::from(u64::MAX)`.
- `check_integrity()` passes for the same reason.
- The validator's range includes `S'` (since `u64::MAX > catch_up_height`). `validate_catch_up_share_content` is called, which calls `pool_reader.get_finalized_block(u64::MAX)` → returns `None` → `ValidationFailure::FinalizedBlockNotFound(u64::MAX)`. The share is left in the unvalidated pool.

**Step 3:** After both shares are inserted, `unvalidated_pool.catch_up_package_share().height_range()` returns `Some(HeightRange { min: Height(0), max: Height(u64::MAX) })`.

**Step 4:** On every purger invocation:
- `below_range_max(catch_up_height, &range)` → `catch_up_height < u64::MAX` → `true`
- `above_range_min(expected_batch_height, &range)` → `expected_batch_height > 0` → `true`
- `return;` — no purge action is emitted.

**Step 5:** The attacker floods the network with additional height-0 `CatchUpPackageShare` messages. Each is accepted into the unvalidated pool, never validated, never purged. Memory grows without bound.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/consensus/src/consensus/purger.rs (L198-203)
```rust
            fn below_range_max(h: Height, range: &Option<HeightRange>) -> bool {
                range.as_ref().map(|r| h < r.max).unwrap_or(false)
            }
            fn above_range_min(h: Height, range: &Option<HeightRange>) -> bool {
                range.as_ref().map(|r| h > r.min).unwrap_or(false)
            }
```

**File:** rs/consensus/src/consensus/purger.rs (L211-218)
```rust
            // Skip purging if we have unprocessed but needed CatchUpPackageShare
            let unvalidated_catch_up_share_range =
                unvalidated_pool.catch_up_package_share().height_range();
            if below_range_max(catch_up_height, &unvalidated_catch_up_share_range)
                && above_range_min(expected_batch_height, &unvalidated_catch_up_share_range)
            {
                return;
            }
```

**File:** rs/consensus/src/consensus/validator.rs (L1600-1617)
```rust
    fn validate_catch_up_package_shares(&self, pool_reader: &PoolReader<'_>) -> Mutations {
        let catch_up_height = pool_reader.get_catch_up_height();
        let max_height = match pool_reader
            .pool()
            .unvalidated()
            .catch_up_package_share()
            .max_height()
        {
            Some(height) => height,
            None => return Mutations::new(),
        };
        let range = HeightRange::new(catch_up_height.increment(), max_height);

        let shares = pool_reader
            .pool()
            .unvalidated()
            .catch_up_package_share()
            .get_by_height_range(range);
```

**File:** rs/consensus/src/consensus/validator.rs (L1649-1658)
```rust
                    Err(ValidationError::ValidationFailed(err)) => {
                        if self.unvalidated_for_too_long(pool_reader, &share.get_id()) {
                            warn!(
                                every_n_seconds => LOG_EVERY_N_SECONDS,
                                self.log,
                                "Couldn't validate the catch-up package share: {:?}", err
                            );
                        }
                        None
                    }
```

**File:** rs/consensus/src/consensus/validator.rs (L1679-1682)
```rust
        let height = share_content.height();
        let block = pool_reader
            .get_finalized_block(height)
            .ok_or(ValidationFailure::FinalizedBlockNotFound(height))?;
```

**File:** rs/types/types/src/consensus.rs (L1737-1741)
```rust
    fn check_integrity(&self) -> bool {
        let content = &self.content;
        let random_beacon_hash = content.random_beacon.get_hash();
        &crypto_hash(content.random_beacon.as_ref()) == random_beacon_hash
    }
```
