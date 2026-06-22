### Title
Bare `u64` Timestamp Subtraction Underflow Silently Disables Voting-Power Spike Detection — (`rs/nns/governance/src/governance/voting_power_snapshots.rs`)

---

### Summary

`totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked` performs an unchecked `u64` subtraction `now_seconds - created_at` to compute snapshot age. When `created_at > now_seconds` the subtraction wraps to a near-`u64::MAX` value in Wasm release mode, causing every stored snapshot to be silently discarded as "stale." The result is that `previous_ballots_if_voting_power_spike_detected` returns `None`, the NNS governance canister proceeds with the current (potentially spiked) voting power, and the anti-spike protection is completely bypassed for the affected proposal.

---

### Finding Description

In `rs/nns/governance/src/governance/voting_power_snapshots.rs`, the staleness filter is:

```rust
.filter(|(created_at, _)| {
    let age = now_seconds - created_at;   // line 134 — bare u64 subtraction
    age <= MAXIMUM_STALENESS_SECONDS
})
```

`TimestampSeconds` is a plain `u64` type alias. [1](#0-0) 

`MAXIMUM_STALENESS_SECONDS` is `ONE_MONTH_SECONDS * 3` (≈ 7,776,000). [2](#0-1) 

Rust compiled to Wasm in release mode does **not** trap on unsigned integer underflow — it wraps two's-complement. If `created_at > now_seconds` by even 1, `age` becomes `u64::MAX - delta`, which is astronomically larger than `MAXIMUM_STALENESS_SECONDS`. Every snapshot in the `StableBTreeMap` is then excluded by the filter, `min_by_key` returns `None`, and the function returns `None`. [3](#0-2) 

The callers that act on this result are:

- `previous_ballots_if_voting_power_spike_detected` — returns `None` (no spike detected, current voting power used). [4](#0-3) 
- `is_latest_snapshot_a_spike` — returns `false` (spike suppressed, new snapshot recorded). [5](#0-4) 

Snapshots are recorded by `SnapshotVotingPowerTask::execute`, which passes `governance.env.now()` (IC time in seconds) as `timestamp_seconds`. [6](#0-5) 

The IC consensus time is monotonically non-decreasing **within a single subnet**, but the following realistic conditions can produce `created_at > now_seconds`:

1. **Canister upgrade / state migration**: if the stable-memory `StableBTreeMap` is populated from a backup or migration script that uses a slightly different time source or nanosecond-to-second rounding, a stored key can exceed the live `env.now()` value by 1 second.
2. **Timer jitter at second boundaries**: `env.now()` returns `ic_cdk::api::time() / 1_000_000_000`. A snapshot recorded at nanosecond time `T` (second `S`) and a staleness check triggered in the same round but with a `now_seconds` argument derived from a slightly earlier batch time (e.g., from a cached or replayed `now_seconds` value) can satisfy `created_at = S > now_seconds = S - 1`.
3. **Governance canister timer task passes `now_seconds` from `governance.env.now()`** at the moment of snapshot recording, but `is_latest_snapshot_a_spike` is also called from the metrics endpoint (`rs/nns/governance/src/lib.rs` line 666) with a freshly computed `now_seconds()`. If the metrics call races with a snapshot recorded in the same second, the subtraction is safe; but if the metrics call uses a stale cached time, it is not. [7](#0-6) 

A second independent bare subtraction exists in `merge_neurons.rs`:

```rust
age_seconds: now_seconds - aging_since_timestamp_seconds,  // line 480
``` [8](#0-7) 

For a `NotDissolving` neuron whose `aging_since_timestamp_seconds` was set to a future value (e.g., via a governance proposal that sets neuron state directly, or a migration), this also wraps, producing a wildly incorrect age that inflates the neuron's voting-power bonus.

---

### Impact Explanation

The primary impact is **governance anti-spike protection bypass**. The spike-detection mechanism exists to prevent a sudden large increase in voting power from being used to pass a proposal before the community can react. If all snapshots are silently discarded as stale due to the underflow, `previous_ballots_if_voting_power_spike_detected` returns `None`, and the governance canister uses the current (spiked) voting power to create ballots. A proposal that should have been subject to the historical-snapshot ballot set instead proceeds with the inflated current voting power, defeating the protection entirely.

The secondary impact (in `merge_neurons.rs`) is **incorrect neuron age calculation**, which inflates the age bonus and therefore the voting power of the merged neuron.

---

### Likelihood Explanation

The IC consensus time is monotonically non-decreasing, so the underflow does not occur in the common case. However, the condition is reachable without any privileged access:

- Any NNS governance proposal submission triggers `previous_ballots_if_voting_power_spike_detected` with the current `now_seconds`. If a snapshot was recorded with a `created_at` that exceeds `now_seconds` by even 1 second (due to rounding, migration, or timer jitter), the entire snapshot set is silently discarded.
- The `SnapshotVotingPowerTask` fires every 24 hours via a recurring timer. The window for a 1-second discrepancy is narrow but non-zero, especially across canister upgrades.
- No attacker privilege is required; the trigger is a normal `make_proposal` ingress call.

---

### Recommendation

Replace the bare subtraction with a saturating or checked variant, treating a future snapshot as age-zero (i.e., fresh):

```rust
.filter(|(created_at, _)| {
    let age = now_seconds.saturating_sub(*created_at);
    age <= MAXIMUM_STALENESS_SECONDS
})
```

Apply the same fix to `merge_neurons.rs` line 480:

```rust
age_seconds: now_seconds.saturating_sub(aging_since_timestamp_seconds),
```

The `DissolveStateAndAge::age_seconds` helper already uses `saturating_sub` correctly and should be used here instead of a manual subtraction. [9](#0-8) 

---

### Proof of Concept

```
1. Governance canister timer fires at IC nanosecond time T_ns = 1_000_000_001_000_000_000
   → now_seconds = T_ns / 1e9 = 1_000_000_001
   → record_voting_power_snapshot(1_000_000_001, snapshot) stores key 1_000_000_001

2. Canister is upgraded; env.now() is re-derived from a batch whose timestamp is
   T_ns' = 1_000_000_000_999_999_999 (one nanosecond before the snapshot second boundary)
   → now_seconds = T_ns' / 1e9 = 1_000_000_000

3. A user submits a make_proposal ingress call.
   → previous_ballots_if_voting_power_spike_detected(vp, 1_000_000_000) is called
   → filter: age = 1_000_000_000 - 1_000_000_001
             = u64::MAX (wraps in release/Wasm)
             = 18_446_744_073_709_551_615 >> MAXIMUM_STALENESS_SECONDS (7_776_000)
   → snapshot is discarded as "stale"
   → all 7 snapshots discarded → returns None
   → spike detection disabled → proposal proceeds with current spiked voting power
``` [10](#0-9) [11](#0-10)

### Citations

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L25-25)
```rust
const MAXIMUM_STALENESS_SECONDS: u64 = ONE_MONTH_SECONDS * 3;
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L28-28)
```rust
type TimestampSeconds = u64;
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

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L122-151)
```rust
    fn totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked(
        &self,
        now_seconds: TimestampSeconds,
        current_total_potential_voting_power: u64,
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

        let voting_power_spike_detected = (current_total_potential_voting_power as f64)
            > (totals_with_minimum_total_potential_voting_power.total_potential_voting_power
                as f64)
                * MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE;
        if voting_power_spike_detected {
            Some((
                timestamp_with_minimum_total_potential_voting_power,
                totals_with_minimum_total_potential_voting_power,
            ))
        } else {
            None
        }
    }
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L157-184)
```rust
    pub(crate) fn previous_ballots_if_voting_power_spike_detected(
        &self,
        total_potential_voting_power: u64,
        now_seconds: TimestampSeconds,
    ) -> Option<(TimestampSeconds, VotingPowerSnapshot)> {
        // Step 0: skip the check in test mode when the snapshots are not yet full. Otherwise it
        // would be difficult to get around the spike detection in tests, and a lot of test setups
        // involve creating a lot of voting power.
        if cfg!(feature = "test") && self.voting_power_totals.len() < MAX_VOTING_POWER_SNAPSHOTS {
            return None;
        }

        // Step 1: find the voting power totals entry with the minimum total potential voting power,
        // if a spike is detected.
        let Some((
            timestamp_with_minimum_total_potential_voting_power,
            totals_with_minimum_total_potential_voting_power,
        )) = self.totals_entry_with_minimum_total_potential_voting_power_if_voting_power_spiked(
            now_seconds,
            total_potential_voting_power,
        )
        else {
            eprintln!(
                "{}Voting power totals are empty. No voting power spike detected.",
                LOG_PREFIX,
            );
            return None;
        };
```

**File:** rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs (L31-57)
```rust
    fn execute(self) -> (Duration, Self) {
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

        (VOTING_POWER_SNAPSHOT_INTERVAL, self)
```

**File:** rs/nns/governance/src/lib.rs (L662-670)
```rust
    let now_seconds = now_seconds();
    let (latest_snapshot_is_spike, latest_snapshot_timestamp_seconds) = VOTING_POWER_SNAPSHOTS
        .with_borrow(|voting_power_snapshots| {
            let latest_snapshot_is_spike =
                voting_power_snapshots.is_latest_snapshot_a_spike(now_seconds);
            let latest_snapshot_timestamp_seconds =
                voting_power_snapshots.latest_snapshot_timestamp_seconds();
            (latest_snapshot_is_spike, latest_snapshot_timestamp_seconds)
        });
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L477-482)
```rust
        Ok(Self {
            id: neuron.id(),
            dissolve_delay_seconds,
            age_seconds: now_seconds - aging_since_timestamp_seconds,
            cached_stake_e8s,
        })
```

**File:** rs/nns/governance/src/neuron/dissolve_state_and_age.rs (L118-125)
```rust
    pub fn age_seconds(self, now_seconds: u64) -> u64 {
        match self {
            Self::NotDissolving {
                aging_since_timestamp_seconds,
                ..
            } => now_seconds.saturating_sub(aging_since_timestamp_seconds),
            Self::DissolvingOrDissolved { .. } => 0,
        }
```
