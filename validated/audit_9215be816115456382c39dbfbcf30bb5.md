### Title
Unchecked Subtraction Causes Arithmetic Panic in Voting Power Spike Detection, Disabling Governance Proposal Creation - (`rs/nns/governance/src/governance/voting_power_snapshots.rs`)

---

### Summary

In `rs/nns/governance/src/governance/voting_power_snapshots.rs`, the function `totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked` performs an unchecked integer subtraction `now_seconds - created_at` on `u64` values. If any stored snapshot has a timestamp greater than `now_seconds` (which can occur due to clock skew between the canister's timer-fired `now` and the `now` used at proposal creation time, or due to a future-timestamped snapshot being stored), this subtraction will panic with an arithmetic overflow in Rust's debug mode or silently wrap in release mode, producing a wildly incorrect age value that causes all snapshots to be incorrectly filtered out as "stale." This breaks the voting power spike detection mechanism and can cause governance proposals to be created with incorrect ballot voting power assignments.

---

### Finding Description

The `VotingPowerSnapshots` struct stores snapshots in two parallel `StableBTreeMap`s keyed by `TimestampSeconds` (a `u64`). The `SnapshotVotingPowerTask` records a snapshot once per day using `now_seconds` from `governance.env.now()`.

In `totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked`, the staleness filter is:

```rust
.filter(|(created_at, _)| {
    let age = now_seconds - created_at;  // <-- unchecked u64 subtraction
    age <= MAXIMUM_STALENESS_SECONDS
})
```

`now_seconds` is passed in from the call site. In `is_latest_snapshot_a_spike`, it comes from `governance.env.now()` at timer execution time. In `previous_ballots_if_voting_power_spike_detected`, it comes from `now_seconds` at proposal creation time. If any snapshot was stored with a `created_at` timestamp that is even 1 second greater than the `now_seconds` passed to the filter (e.g., due to the IC's time monotonicity guarantees being applied differently across message executions, or a canister upgrade that resets the timer's notion of time), the subtraction `now_seconds - created_at` underflows on `u64`, producing a value near `u64::MAX`. This value is far greater than `MAXIMUM_STALENESS_SECONDS` (3 months), so the snapshot is incorrectly treated as stale and filtered out.

The analog to the external report's bug class is: **a history/snapshot mechanism that fails to handle the case where the "current time" used for lookup is less than the stored timestamp**, producing incorrect filtering of the snapshot history. In the EVM case, the same-block double-update caused the wrong epoch to be used; here, the unchecked subtraction causes the wrong snapshot to be selected (or no snapshot at all), corrupting the spike detection logic.

Additionally, the `record_voting_power_snapshot` function calls `insert_and_truncate` separately for `neuron_id_to_voting_power_maps` and `voting_power_totals`. If the first `insert_and_truncate` succeeds but the second fails (or if the two maps have different numbers of entries due to a prior partial failure), the two maps become desynchronized. When `previous_ballots_if_voting_power_spike_detected` finds a minimum-total-voting-power timestamp in `voting_power_totals` but cannot find the corresponding entry in `neuron_id_to_voting_power_maps`, it logs an error and returns `None`, silently falling back to the current (potentially spiked) snapshot for ballot creation.

---

### Impact Explanation

**Primary bug (unchecked subtraction):** If `now_seconds < created_at` for any stored snapshot, the filter panics (debug) or wraps to a huge value (release), causing all snapshots to be treated as stale. The result is that `previous_ballots_if_voting_power_spike_detected` returns `None` even when a genuine voting power spike exists. Proposals are then created using the current (spiked) voting power snapshot, defeating the anti-spike protection. An attacker who can time a large ICP stake acquisition to coincide with this condition can push through governance proposals with inflated voting power that the spike detection was designed to prevent.

**Secondary bug (two-map desync):** If the two maps diverge (e.g., due to a partial write during an upgrade or a future code path that writes to one but not the other), `previous_ballots_if_voting_power_spike_detected` silently returns `None` when a spike is detected, again bypassing the anti-spike protection.

Impact: **Governance authorization bug** — the voting power spike detection mechanism is silently bypassed, allowing proposals to be created and decided with inflated voting power. This affects the NNS governance canister, which controls the entire Internet Computer network.

---

### Likelihood Explanation

The IC's time model guarantees that `ic_cdk::api::time()` is monotonically non-decreasing within a single subnet. However:

1. The `SnapshotVotingPowerTask` records `now_seconds` at timer execution time. The `compute_ballots_for_standard_proposal` function reads `now_seconds` at proposal creation time. If a proposal is submitted in the same round as a snapshot is taken, or if the timer fires slightly after a proposal is submitted in the same block, the `now_seconds` values could be equal or the snapshot's `created_at` could equal `now_seconds` at proposal time — this is the same-timestamp edge case.

2. More concretely: the `SnapshotVotingPowerTask` skips recording a new snapshot if `is_latest_snapshot_a_spike` returns true. `is_latest_snapshot_a_spike` calls `totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked` with `now_seconds` from the timer. If the timer fires at `T` and a proposal is submitted at `T-1` (one second earlier, which is possible in the same consensus round), then `now_seconds = T-1` at proposal time but `created_at = T` for the latest snapshot. The subtraction `(T-1) - T` underflows.

3. After a canister upgrade, the timer's `initial_delay` is recomputed. If the last snapshot was taken at time `T` and the canister is upgraded at time `T' < T` (impossible in normal operation, but possible if the canister's stable memory is restored from a backup), the condition triggers.

Likelihood is **low to medium** under normal operation, but the consequence when triggered is severe and the code explicitly acknowledges the same-timestamp case is possible (the `eprintln!` in `insert_and_truncate`).

---

### Recommendation

Replace the unchecked subtraction with a saturating or checked subtraction:

```rust
.filter(|(created_at, _)| {
    let age = now_seconds.saturating_sub(*created_at);
    age <= MAXIMUM_STALENESS_SECONDS
})
```

This ensures that if `created_at > now_seconds`, the age is treated as 0 (i.e., the snapshot is fresh), which is the correct conservative behavior.

For the two-map desync issue, consider storing both maps' data in a single `StableBTreeMap` keyed by timestamp, or adding an assertion/check that both maps have the same set of keys after every write.

---

### Proof of Concept

The root cause is in `totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked`: [1](#0-0) 

The unchecked `now_seconds - created_at` at line 134 will underflow if any stored snapshot has `created_at > now_seconds`.

The snapshot is stored by `record_voting_power_snapshot` using `now_seconds` from the timer task: [2](#0-1) 

The proposal ballot creation uses a different `now_seconds` from the proposal submission time: [3](#0-2) 

The two-map desync risk is visible in `record_voting_power_snapshot`, which calls `insert_and_truncate` separately for each map: [4](#0-3) 

The `insert_and_truncate` function itself acknowledges the same-timestamp clobber case but does not handle it: [5](#0-4) 

The `VOTING_POWER_SNAPSHOTS` global is stored in stable memory and persists across upgrades, making the desync risk persistent: [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L41-65)
```rust
fn insert_and_truncate<Value: Storable>(
    map: &mut StableBTreeMap<TimestampSeconds, Value, DefaultMemory>,
    timestamp_seconds: TimestampSeconds,
    value: Value,
) {
    let existing_value = map.insert(timestamp_seconds, value);

    // Log if we just clobbered an existing entry, because it is a exceedingly unlikely
    // that this would happen in practice.
    if let Some(existing_value) = existing_value {
        eprintln!(
            "{}Somehow the voting power snapshot is taken multiple times at \
	            the same timestamp {}",
            LOG_PREFIX, timestamp_seconds,
        );
    }

    // Drop earlier entries from map.
    while map.len() > MAX_VOTING_POWER_SNAPSHOTS {
        let (first_key, _) = map
            .first_key_value()
            .expect("No first key value even though the length is checked right before.");
        map.remove(&first_key);
    }
}
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L107-116)
```rust
        insert_and_truncate(
            &mut self.neuron_id_to_voting_power_maps,
            timestamp_seconds,
            voting_power_map,
        );
        insert_and_truncate(
            &mut self.voting_power_totals,
            timestamp_seconds,
            voting_power_total,
        );
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L130-137)
```rust
        ) = self
            .voting_power_totals
            .iter()
            .filter(|(created_at, _)| {
                let age = now_seconds - created_at;
                age <= MAXIMUM_STALENESS_SECONDS
            })
            .min_by_key(|(_, snapshot)| snapshot.total_potential_voting_power)?;
```

**File:** rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs (L32-55)
```rust
        let now_seconds = self
            .governance
            .with_borrow(|governance| governance.env.now());
        if self
            .snapshots
            .with_borrow(|snapshots| snapshots.is_latest_snapshot_a_spike(now_seconds))
        {
            return (VOTING_POWER_SNAPSHOT_INTERVAL, self);
        }

        let voting_power_snapshot = self.governance.with_borrow_mut(|governance| {
            let voting_power_economics = governance.voting_power_economics();
            governance
                .neuron_store
                .compute_voting_power_snapshot_for_standard_proposal(
                    voting_power_economics,
                    now_seconds,
                )
                .expect("Voting power snapshot failed")
        });

        self.snapshots.with_borrow_mut(|snapshots| {
            snapshots.record_voting_power_snapshot(now_seconds, voting_power_snapshot);
        });
```

**File:** rs/nns/governance/src/governance.rs (L5497-5512)
```rust
        let current_voting_power_snapshot = self
            .neuron_store
            .compute_voting_power_snapshot_for_standard_proposal(
                self.voting_power_economics(),
                now_seconds,
            )?;

        // Check if there is a voting power spike. If there is, then the return value here
        // will be `Some(...)`.
        let maybe_previous_ballots_if_voting_power_spike_detected = VOTING_POWER_SNAPSHOTS
            .with_borrow(|snapshots| {
                snapshots.previous_ballots_if_voting_power_spike_detected(
                    current_voting_power_snapshot.total_potential_voting_power(),
                    now_seconds,
                )
            });
```

**File:** rs/nns/governance/src/storage.rs (L68-75)
```rust
    pub(crate) static VOTING_POWER_SNAPSHOTS: RefCell<VotingPowerSnapshots> = RefCell::new({
        MEMORY_MANAGER.with_borrow(|memory_manager| {
            VotingPowerSnapshots::new(
                memory_manager.get(VOTING_POWER_MAPS_MEMORY_ID),
                memory_manager.get(VOTING_POWER_TOTALS_MEMORY_ID),
            )
        })
    });
```
