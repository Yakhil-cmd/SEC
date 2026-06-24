### Title
Integer Underflow in Staleness Filter Silently Disables Voting Power Spike Detection - (File: `rs/nns/governance/src/governance/voting_power_snapshots.rs`)

### Summary
A bare `u64` subtraction in the snapshot staleness filter inside `totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked` can underflow in Wasm release mode, wrapping to a very large value. This causes every stored snapshot to be classified as "too stale" and filtered out, making the spike-detection iterator empty and returning `None` — silently bypassing the voting-power spike guard on every subsequent governance proposal.

### Finding Description
In `rs/nns/governance/src/governance/voting_power_snapshots.rs` the staleness filter is:

```rust
.filter(|(created_at, _)| {
    let age = now_seconds - created_at;   // line 134 — bare u64 subtraction
    age <= MAXIMUM_STALENESS_SECONDS
})
```

`TimestampSeconds` is `u64`. [1](#0-0) 

If `created_at > now_seconds` — which can occur after a canister upgrade that restores stable memory containing snapshots whose timestamps are ahead of the current IC time, or after any time regression — the subtraction wraps to `u64::MAX - (created_at - now_seconds)`. That value is always larger than `MAXIMUM_STALENESS_SECONDS` (3 months in seconds), so the filter predicate returns `false` for every snapshot. The iterator is empty, `min_by_key` returns `None`, and the function returns `None` — meaning "no spike detected". [2](#0-1) 

The same function is the sole staleness gate used by both `is_latest_snapshot_a_spike` and `previous_ballots_if_voting_power_spike_detected`. [3](#0-2) 

Note that the `initial_delay` helper in `SnapshotVotingPowerTask` correctly uses `saturating_sub` for the same kind of arithmetic, showing the pattern is known but was not applied here. [4](#0-3) 

### Impact Explanation
The voting-power spike detection is the NNS governance mechanism that prevents a newly minted or transferred large stake from immediately passing proposals by comparing current total potential voting power against a rolling window of historical snapshots. [5](#0-4) 

When the filter underflows and returns `None`, `compute_ballots_for_standard_proposal` falls through to the `None` branch and uses the current (spiked) snapshot for ballot creation. [6](#0-5) 

An attacker who controls a neuron with a large stake — acquired just before the time regression window — can submit a governance proposal that would normally be blocked by spike detection. With the guard silently disabled, the proposal is created with inflated voting power and can be adopted immediately if the attacker's stake exceeds the adoption threshold. This is a **governance authorization bypass**.

### Likelihood Explanation
The IC batch time is monotonically non-decreasing within a running subnet, so `created_at > now_seconds` does not occur under normal operation. However, the condition is reachable via:

1. **Canister upgrade / state restore**: If the governance canister is upgraded and its stable memory (which holds `VOTING_POWER_SNAPSHOTS` in `StableBTreeMap`) is restored from a backup whose snapshot timestamps are ahead of the current subnet time (e.g., after a subnet rollback or disaster recovery), `created_at` will exceed `now_seconds` for every stored entry. [7](#0-6) 
2. **Subnet migration**: Moving the governance canister to a subnet whose certified time is behind the timestamps already stored in stable memory.

Both scenarios are operationally plausible during incident response or subnet maintenance, and the NNS governance canister is a high-value target.

### Recommendation
Replace the bare subtraction with `saturating_sub`, consistent with the pattern already used elsewhere in the same file's task helper:

```rust
.filter(|(created_at, _)| {
    let age = now_seconds.saturating_sub(*created_at);
    age <= MAXIMUM_STALENESS_SECONDS
})
```

This ensures that if `created_at > now_seconds` the age is treated as `0` (i.e., the snapshot is considered fresh), which is the safe and correct fallback — a snapshot from the "future" should not be discarded as stale.

### Proof of Concept

1. Populate `VOTING_POWER_SNAPSHOTS` with 7 daily snapshots, each with `timestamp_seconds = T` where `T` is the current IC time.
2. Trigger a subnet rollback or canister state restore so that `governance.env.now()` returns `T - δ` (any positive `δ`).
3. Call `make_proposal` from an unprivileged ingress sender controlling a neuron whose stake was just increased to create a >1.5× spike.
4. `compute_ballots_for_standard_proposal` calls `previous_ballots_if_voting_power_spike_detected(current_vp, T - δ)`.
5. Inside `totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked`, for every stored entry `created_at = T`: `age = (T - δ) - T` wraps to `u64::MAX - δ + 1`, which is >> `MAXIMUM_STALENESS_SECONDS`.
6. All snapshots are filtered out; the function returns `None`; spike detection is bypassed.
7. The proposal is created with the current spiked voting power and can be adopted immediately. [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L19-25)
```rust
/// power is more than this multiplier times the minimum total voting power in the snapshots,
/// then we consider it a spike.
const MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE: f64 = 1.5;
/// The maximum staleness of a voting power snapshot. This is usually not needed since
/// the snapshots should be added frequently. However, we do not want to use a snapshot that is too
/// old, in the event of a failure in taking the snapshots.
const MAXIMUM_STALENESS_SECONDS: u64 = ONE_MONTH_SECONDS * 3;
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L35-39)
```rust
pub(crate) struct VotingPowerSnapshots {
    neuron_id_to_voting_power_maps:
        StableBTreeMap<TimestampSeconds, NeuronIdToVotingPowerMap, DefaultMemory>,
    voting_power_totals: StableBTreeMap<TimestampSeconds, VotingPowerTotal, DefaultMemory>,
}
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L76-87)
```rust
    pub fn is_latest_snapshot_a_spike(&self, now_seconds: TimestampSeconds) -> bool {
        // If there are no snapshots, then there is no spike.
        let Some((_, latest_totals)) = self.voting_power_totals.last_key_value() else {
            return false;
        };

        self.totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked(
            now_seconds,
            latest_totals.total_potential_voting_power,
        )
        .is_some()
    }
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L126-137)
```rust
    ) -> Option<(TimestampSeconds, VotingPowerTotal)> {
        let (
            timestamp_with_minimum_total_potential_voting_power,
            totals_with_minimum_total_potential_voting_power,
        ) = self
            .voting_power_totals
            .iter()
            .filter(|(created_at, _)| {
                let age = now_seconds - created_at;
                age <= MAXIMUM_STALENESS_SECONDS
            })
            .min_by_key(|(_, snapshot)| snapshot.total_potential_voting_power)?;
```

**File:** rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs (L69-72)
```rust
                let next_snapshot_timestamp_seconds = last_snapshot_timestamp_seconds
                    .saturating_add(VOTING_POWER_SNAPSHOT_INTERVAL.as_secs());
                let delay_seconds = next_snapshot_timestamp_seconds.saturating_sub(now_seconds);
                Duration::from_secs(delay_seconds)
```

**File:** rs/nns/governance/src/governance.rs (L5514-5524)
```rust
        let (voting_power_snapshot, previous_ballots_timestamp_seconds) =
            match maybe_previous_ballots_if_voting_power_spike_detected {
                // This is the extraordinary case - we have a voting power spike, and we
                // need to use the previous snapshot.
                Some((previous_snapshot_timestamp, previous_snapshot)) => {
                    (previous_snapshot, Some(previous_snapshot_timestamp))
                }
                // This is the normal case - we have no voting power spike, so we use the
                // current snapshot.
                None => (current_voting_power_snapshot, None),
            };
```
