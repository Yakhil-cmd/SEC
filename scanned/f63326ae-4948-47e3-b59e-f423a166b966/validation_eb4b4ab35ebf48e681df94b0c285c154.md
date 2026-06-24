### Title
Voting Power Spike Detection Uses Previous Snapshot Ballots That Exclude Neurons Created After Snapshot Timestamp, Permanently Disenfranchising Legitimate Voters - (`rs/nns/governance/src/governance/voting_power_snapshots.rs`, `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS governance canister implements a "voting power spike" detection mechanism that, when triggered, substitutes a stale historical snapshot of neuron voting power for the current one when creating proposal ballots. Any neuron that was created (or became eligible to vote) after the timestamp of the selected historical snapshot is permanently excluded from voting on that proposal — it receives no ballot entry and cannot vote, even though it is a fully valid, eligible neuron at proposal creation time.

An unprivileged user can deliberately trigger this condition by staking a large amount of ICP into a new neuron (or by refreshing an existing neuron's voting power to inflate the current snapshot's total), causing the spike detector to fall back to a stale snapshot that excludes recently-created neurons from the ballot.

---

### Finding Description

The NNS governance canister periodically (once per day) records a `VotingPowerSnapshot` via `SnapshotVotingPowerTask`. Up to 7 snapshots are retained, each keyed by timestamp. [1](#0-0) 

When a new proposal is created, `compute_ballots_for_standard_proposal` computes the current snapshot and checks whether the current total potential voting power is more than **1.5×** the minimum total potential voting power across all retained snapshots. If so, a "spike" is detected and the **historical snapshot with the minimum total potential voting power** is used to create the proposal's ballots instead of the current one. [2](#0-1) 

The historical snapshot is a frozen map of `neuron_id → deciding_voting_power` taken at a past timestamp. Any neuron that did not exist (or was not eligible) at that past timestamp is simply absent from the map. When `create_ballots_and_total_potential_voting_power` converts the snapshot into ballots, it only creates ballot entries for neurons present in the map: [3](#0-2) 

The `register_vote` path in NNS governance checks whether the neuron has a ballot entry in `proposal.ballots`. If the neuron is absent, it returns `NotAuthorized`: [4](#0-3) 

This is the same root cause as the Party Governance analog: a security mitigation (spike detection → use old snapshot) introduces a side-effect where a state change (large neuron creation) causes legitimate voters to be silently excluded from a proposal's ballot.

The `SnapshotVotingPowerTask` also refuses to record a new snapshot when the latest snapshot is already a spike, meaning the stale-snapshot condition can persist across multiple consecutive days: [5](#0-4) 

---

### Impact Explanation

Any neuron created (or that became eligible to vote) after the timestamp of the selected historical snapshot is **permanently excluded from voting** on any proposal created while the spike condition persists. This is a governance authorization bug:

- Legitimate ICP stakers who created neurons between the historical snapshot timestamp and the proposal creation time cannot vote on those proposals.
- The proposer's own neuron (if newly created) would also be excluded from the ballot, meaning the proposer cannot vote on their own proposal.
- The spike condition can persist for multiple days (since `SnapshotVotingPowerTask` skips recording new snapshots while a spike is present), compounding the disenfranchisement across all proposals created during that window.
- The `total_potential_voting_power` stored in the proposal is taken from the stale snapshot, which is lower than the actual current total, potentially making it easier for the attacker's pre-existing neurons (which ARE in the old snapshot) to reach the early-adoption majority threshold.

---

### Likelihood Explanation

The trigger condition is reachable by any unprivileged ICP holder:

1. An attacker stakes enough ICP to create a neuron with voting power exceeding 50% of the minimum historical snapshot total (i.e., enough to push the current total above 1.5× the minimum snapshot total).
2. The attacker submits a proposal immediately after staking.
3. The spike detector fires, the old snapshot is used, and the attacker's own neuron (and all other recently-created neurons) are excluded from the ballot.
4. The attacker's pre-existing neurons (present in the old snapshot) can then vote on the proposal with a reduced effective total voting power denominator.

The 1.5× threshold is not prohibitively high for a well-resourced attacker. The NNS has hundreds of millions of ICP staked; an attacker would need to stake enough to push the current total above 1.5× the minimum of the last 7 daily snapshots. This is a significant but not impossible capital requirement for a targeted attack on a specific proposal.

---

### Recommendation

1. **When a spike is detected, include all currently-eligible neurons in the ballot** (using the current snapshot), but cap each neuron's ballot voting power at its value in the historical snapshot (or at 0 if it was not present). This prevents new neurons from inflating the total while still allowing them to participate.

2. Alternatively, **do not use the historical snapshot for ballot creation**; instead, use the current snapshot but set the `total` field in the tally to the historical minimum, so that the early-adoption threshold is harder to reach without excluding any neuron from voting.

3. At minimum, **document that neurons created after the historical snapshot timestamp will be unable to vote** on proposals created during a spike window, so that users are aware of the limitation.

---

### Proof of Concept

**Setup:** The NNS has been running for 7+ days, so 7 daily snapshots exist. The minimum total potential voting power across those snapshots is `T_min`.

**Attack:**

1. Attacker stakes `> 0.5 * T_min` ICP into a new neuron with a dissolve delay ≥ the minimum required. This pushes the current total potential voting power above `1.5 * T_min`.

2. Attacker (or any user) calls `manage_neuron` → `MakeProposal` immediately.

3. Inside `compute_ballots_for_standard_proposal`:
   - `current_voting_power_snapshot.total_potential_voting_power()` > `1.5 * T_min` → spike detected.
   - `previous_ballots_if_voting_power_spike_detected` returns the snapshot at the timestamp with minimum total potential voting power (e.g., 7 days ago).
   - Ballots are created from the 7-day-old snapshot.

4. The attacker's new neuron (and all neurons created in the last 7 days) have **no ballot entry** in the proposal.

5. `register_vote` for any of these neurons returns `NotAuthorized` ("Neuron not authorized to vote on proposal.").

6. `SnapshotVotingPowerTask` skips recording new snapshots while the spike persists, so all proposals created the next day also use the stale snapshot. [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L16-21)
```rust
/// The maximum number of voting power snapshots to keep.
const MAX_VOTING_POWER_SNAPSHOTS: u64 = 7;
/// The multiplier used to define what is a "voting power spike": if the current total voting
/// power is more than this multiplier times the minimum total voting power in the snapshots,
/// then we consider it a spike.
const MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE: f64 = 1.5;
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L119-151)
```rust
    /// Given a total potential voting power, checks if there is a voting power spike. If a spike is
    /// detected, it returns the timestamp and totals of the snapshot with the minimum total
    /// potential voting power. If no spike is detected, it returns None.
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

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L157-217)
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

        // Step 2: find the voting power map for the timestamp with the minimum potential voting power.
        let Some(voting_power_map) = self
            .neuron_id_to_voting_power_maps
            .get(&timestamp_with_minimum_total_potential_voting_power)
        else {
            eprintln!(
                "{}Voting power map not found for timestamp {} while the totals \
                are found. This should not happen.",
                LOG_PREFIX, timestamp_with_minimum_total_potential_voting_power,
            );
            return None;
        };

        // Step 3: returns one of the previous voting power maps (with minimum total potential
        // voting power) since a voting power spike is detected.
        let previous_voting_power_snapshot = VotingPowerSnapshot::from((
            voting_power_map,
            totals_with_minimum_total_potential_voting_power,
        ));
        println!(
            "{}Voting power spike detected at timestamp {}, total potential voting power: {}, \
            minimum total potential voting power: {}",
            LOG_PREFIX,
            timestamp_with_minimum_total_potential_voting_power,
            total_potential_voting_power,
            totals_with_minimum_total_potential_voting_power.total_potential_voting_power
        );
        Some((
            timestamp_with_minimum_total_potential_voting_power,
            previous_voting_power_snapshot,
        ))
    }
```

**File:** rs/nns/governance/src/governance.rs (L5486-5533)
```rust
    fn compute_ballots_for_standard_proposal(
        &self,
        now_seconds: u64,
    ) -> Result<
        (
            HashMap<u64, Ballot>,
            u64,         /*potential_voting_power*/
            Option<u64>, /*previous_ballots_timestamp_seconds*/
        ),
        GovernanceError,
    > {
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

        let (ballots, total_potential_voting_power) =
            voting_power_snapshot.create_ballots_and_total_potential_voting_power();
        Ok((
            ballots,
            total_potential_voting_power,
            previous_ballots_timestamp_seconds,
        ))
    }
```

**File:** rs/nns/governance/src/governance.rs (L5639-5648)
```rust
        let neuron_ballot = proposal.ballots.get_mut(&neuron_id.id).ok_or_else(||
            // This neuron is not eligible to vote on this proposal.
            GovernanceError::new_with_message(ErrorType::NotAuthorized, "Neuron not authorized to vote on proposal."))?;
        if neuron_ballot.vote != (Vote::Unspecified as i32) {
            // Already voted.
            return Err(GovernanceError::new_with_message(
                ErrorType::NeuronAlreadyVoted,
                "Neuron already voted on proposal.",
            ));
        }
```

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L57-70)
```rust
        let ballots = voting_power_map
            .into_iter()
            .map(|(neuron_id, voting_power)| {
                (
                    neuron_id,
                    Ballot {
                        voting_power,
                        vote: Vote::Unspecified as i32,
                    },
                )
            })
            .collect();

        (ballots, total_potential_voting_power)
```

**File:** rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs (L30-57)
```rust
impl RecurringSyncTask for SnapshotVotingPowerTask {
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
