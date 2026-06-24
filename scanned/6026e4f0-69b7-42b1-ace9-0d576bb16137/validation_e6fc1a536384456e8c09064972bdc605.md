### Title
Stale Voting Power Snapshot Used for Ballot Creation Allows Neuron State Changes After Snapshot to Inflate Voting Power and Rewards - (File: `rs/nns/governance/src/governance/voting_power_snapshots.rs`)

### Summary

The NNS Governance canister takes daily snapshots of neuron voting power to detect "voting power spikes." When a spike is detected at proposal creation time, ballots are initialized from a **previous** (older) snapshot rather than the current live state. Because the snapshot is a point-in-time capture that does not reflect neuron state changes occurring after it was taken, a neuron owner can deliberately increase their stake **after** the snapshot is recorded but **before** a proposal is created, causing the proposal to use the stale (lower) snapshot for ballot initialization. This means the attacker's neuron is assigned a ballot with the old (lower) voting power, while the attacker's actual current voting power is higher. The attacker then votes with the stale ballot, earns voting rewards proportional to the stale ballot's voting power, and simultaneously holds the full current stake. The inverse scenario — where a neuron's stake is reduced after the snapshot — causes the snapshot to overstate the neuron's voting power relative to its current state, allowing a neuron that has dissolved or reduced its stake to still receive rewards based on the inflated snapshot value.

### Finding Description

The `VotingPowerSnapshots` struct in `rs/nns/governance/src/governance/voting_power_snapshots.rs` stores up to 7 daily snapshots of neuron voting power. When `compute_ballots_for_standard_proposal` is called during proposal creation, it first computes the current live snapshot, then checks whether the current total potential voting power exceeds 1.5× the minimum total potential voting power across all stored snapshots. If a spike is detected, the **previous snapshot with the minimum total potential voting power** is used to initialize ballots instead of the current live state.

The critical issue is that the snapshot used for ballot initialization is **stale**: it reflects neuron states from a past point in time (up to 7 days old, or up to 3 months old per `MAXIMUM_STALENESS_SECONDS`). Between the time the snapshot was taken and the time the proposal is created, neuron state can change materially:

- A neuron can **increase** its stake (by topping up ICP), causing the snapshot to understate its current voting power. The neuron receives a ballot with the old lower voting power, votes, and earns rewards proportional to that lower power — but the attacker retains the full current stake.
- A neuron can **decrease** its stake or dissolve, causing the snapshot to **overstate** its voting power. The neuron receives a ballot with the old higher voting power, votes, and earns rewards proportional to that higher power — even though the neuron no longer holds that stake.

The second scenario is the direct analog of the external report: a user's state at the snapshot time is used to credit them with rewards, even though their actual state has changed after the snapshot.

The spike detection logic in `previous_ballots_if_voting_power_spike_detected` selects the snapshot with the **minimum** total potential voting power among all non-stale snapshots:

```rust
.min_by_key(|(_, snapshot)| snapshot.total_potential_voting_power)?;
```

This means the selected snapshot can be up to 7 days old (one snapshot per day, 7 max). The `MAXIMUM_STALENESS_SECONDS` constant is set to 3 months, meaning snapshots up to 3 months old can be used if the daily task fails.

The `SnapshotVotingPowerTask` skips recording a new snapshot if the latest snapshot is already a spike, meaning a sustained spike condition can cause the snapshot store to remain frozen at an old state indefinitely.

The reward distribution in `distribute_voting_rewards_to_neurons` uses the ballot's `voting_power` field (set at proposal creation from the snapshot) to determine each neuron's share of rewards. Since ballots are cleared after reward distribution, there is no second check against current neuron state.

### Impact Explanation

**Governance authorization bug / ledger conservation bug.**

An unprivileged neuron owner can:

1. **Inflate rewards**: Dissolve or reduce their neuron's stake after a snapshot is taken. When a spike is detected and the old snapshot is used for ballot creation, the neuron receives a ballot with the old (higher) voting power. The neuron votes, earns maturity rewards proportional to the inflated ballot, and has already withdrawn the stake. This is a direct analog of the external report's double-claim: the user withdraws their deposit and is still credited for it in the reward distribution.

2. **Suppress other neurons' rewards**: By engineering a spike condition (e.g., by temporarily staking a large amount to inflate the current total, then withdrawing), the attacker forces the system to use an old snapshot. Neurons that gained voting power since the old snapshot are assigned lower ballot weights, reducing their reward share.

The maturity credited to neurons is eventually minted as ICP tokens via `DisburseMaturity`, so this represents a real ledger conservation violation: more ICP can be minted than the governance token supply justifies.

### Likelihood Explanation

**Medium.** The spike detection threshold is 1.5× the minimum snapshot total. An attacker needs to cause the current total potential voting power to exceed 1.5× the minimum stored snapshot total. This requires either:
- Coordinating a large stake increase (e.g., staking enough ICP to push the total above the threshold), or
- Waiting for organic network growth to cause a spike, then timing their stake reduction to coincide with a proposal creation during the spike window.

The 7-day snapshot window and the fact that the `SnapshotVotingPowerTask` skips recording during a spike (keeping the store frozen) make the window of exploitation potentially long. The attacker does not need any privileged access — only the ability to stake and unstake ICP neurons, which is a standard unprivileged operation.

### Recommendation

1. **Validate ballot voting power against current neuron state at reward distribution time**: Before crediting a neuron with rewards, verify that the neuron still exists and cap the reward at the neuron's current stake-based voting power.

2. **Reduce snapshot staleness**: Reduce `MAXIMUM_STALENESS_SECONDS` and `MAX_VOTING_POWER_SNAPSHOTS` to limit how old a snapshot can be when used for ballot creation.

3. **Invalidate ballots for dissolved/reduced neurons**: At reward settlement time in `distribute_voting_rewards_to_neurons`, check whether the neuron's current stake is consistent with its ballot voting power, and reduce the reward proportionally if the stake has decreased.

4. **Do not freeze the snapshot store during a spike**: The current logic in `SnapshotVotingPowerTask` skips recording a new snapshot when the latest is a spike, which can cause the store to remain frozen indefinitely. New snapshots should still be recorded (or the spike condition should be re-evaluated more frequently) to prevent the store from becoming arbitrarily stale.

### Proof of Concept

**Scenario: Neuron reduces stake after snapshot, earns inflated rewards**

1. At time T=0, the daily `SnapshotVotingPowerTask` records a snapshot. Attacker's neuron N has 10,000 ICP staked → voting power VP_old = 10,000 (simplified).

2. At time T=1 day, a new snapshot is recorded. Attacker's neuron still has 10,000 ICP.

3. At time T=6 days, 7 snapshots exist. Attacker's neuron still has 10,000 ICP in all snapshots. Minimum snapshot total = X.

4. At time T=7 days, attacker stakes an additional 100,000 ICP into a new neuron, causing the current total potential voting power to exceed 1.5×X (a spike). The `SnapshotVotingPowerTask` detects the spike and **skips** recording a new snapshot.

5. Attacker immediately dissolves their original neuron N (or reduces its stake to near zero), withdrawing the 10,000 ICP.

6. Attacker (or any user) creates a standard proposal. `compute_ballots_for_standard_proposal` detects the spike and calls `previous_ballots_if_voting_power_spike_detected`, which returns the snapshot from step 1 (minimum total). Neuron N receives a ballot with VP_old = 10,000, even though it now has ~0 ICP staked.

7. Attacker votes on the proposal using neuron N's ballot (the neuron still exists, just with reduced stake; voting is permitted as long as the neuron has a ballot).

8. At reward distribution time, `distribute_voting_rewards_to_neurons` credits neuron N with maturity proportional to VP_old = 10,000. The attacker has already withdrawn the 10,000 ICP and now also receives maturity rewards as if they still held it.

9. Attacker calls `DisburseMaturity` on neuron N, minting ICP from the governance canister's minting account — ICP that was not backed by the neuron's current stake.

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L16-25)
```rust
/// The maximum number of voting power snapshots to keep.
const MAX_VOTING_POWER_SNAPSHOTS: u64 = 7;
/// The multiplier used to define what is a "voting power spike": if the current total voting
/// power is more than this multiplier times the minimum total voting power in the snapshots,
/// then we consider it a spike.
const MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE: f64 = 1.5;
/// The maximum staleness of a voting power snapshot. This is usually not needed since
/// the snapshots should be added frequently. However, we do not want to use a snapshot that is too
/// old, in the event of a failure in taking the snapshots.
const MAXIMUM_STALENESS_SECONDS: u64 = ONE_MONTH_SECONDS * 3;
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

**File:** rs/nns/governance/src/governance.rs (L6762-6853)
```rust
    /// Distributes voting rewards to neurons.
    ///
    /// This method:
    /// * collects all proposals in state ReadyToSettle, that is, proposals that
    ///   can no longer accept votes for the purpose of rewards and that have
    ///   not yet been considered in a reward event.
    /// * calculates the voting rewards to distribute.
    /// * schedules the rewards distribution.
    /// * updates the reward event.
    /// * updates the proposals from ReadyToSettle to Settled.
    pub(crate) fn distribute_voting_rewards_to_neurons(&mut self, supply: Tokens) {
        println!(
            "{}distribute_voting_rewards_to_neurons. Supply: {:?}",
            LOG_PREFIX, supply
        );

        let voting_rewards_calculation_result = self.calculate_voting_rewards(supply);
        let Some((new_reward_event, reward_distribution)) = voting_rewards_calculation_result
        else {
            return;
        };

        // Now the mutations begin. Once any mutation has happened, we cannot exit early without the
        // rest. Otherwise we could end up in an inconsistent state and break some properties we
        // would like to hold.
        //
        // The properties we would like to hold are:
        // * The rewards for a given day is only distributed once. This is made sure by updating the
        //   reward event, in particular the `day_after_genesis` field every time
        //   `schedule_pending_rewards_distribution` is called. This is also the main reason that
        //   once mutations begin, we should not exit early before  `latest_reward_event` is
        //   updated.
        // * The proposals should only be settled once. This is made sure by updating the proposal
        //   status from `ReadyToSettle` to `Settled`.

        if let Some(reward_distribution) = reward_distribution {
            self.schedule_pending_rewards_distribution(
                new_reward_event.day_after_genesis,
                reward_distribution,
            );
        }

        let known_neuron_ids = self.neuron_store.list_known_neuron_ids();

        // Mark the proposals that we just considered as "rewarded". More
        // formally, causes their reward_status to be Settled; whereas, before,
        // they were in the ReadyToSettle state.
        for pid in new_reward_event.settled_proposals.iter() {
            // Before considering a proposal for reward, it must be fully processed --
            // because we're about to clear the ballots, so no further processing will be
            // possible.
            self.process_proposal(pid.id);

            match self.mut_proposal_data(*pid) {
                None => println!(
                    "{}Cannot find proposal {}, despite it being considered for rewards distribution.",
                    LOG_PREFIX, pid.id
                ),
                Some(p) => {
                    if p.status() == ProposalStatus::Open {
                        println!(
                            "{}Proposal {} was considered for reward distribution despite \
                          being open. This code line is expected not to be reachable. We need to \
                          clear the ballots here to avoid a risk of the memory getting too large. \
                          In doubt, reject the proposal",
                            LOG_PREFIX, pid.id
                        );
                        p.decided_timestamp_seconds = new_reward_event.actual_timestamp_seconds;
                        p.latest_tally = Some(Tally {
                            timestamp_seconds: new_reward_event.actual_timestamp_seconds,
                            yes: 0,
                            no: 0,
                            total: 0,
                        })
                    };
                    p.reward_event_round = new_reward_event.day_after_genesis;
                    let ballots = std::mem::take(&mut p.ballots);
                    record_known_neuron_abstentions(&known_neuron_ids, *pid, ballots);
                }
            };
        }

        if new_reward_event.settled_proposals.is_empty() {
            println!(
                "{}Voting rewards will roll over, because no there were proposals \
                 that needed rewards (i.e. have reward_status == ReadyToSettle)",
                LOG_PREFIX,
            );
        };

        self.heap_data.latest_reward_event = Some(new_reward_event);
    }
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

**File:** rs/nns/governance/src/reward/distribution.rs (L154-188)
```rust
    fn continue_processing(
        &mut self,
        neuron_store: &mut NeuronStore,
        is_over_instructions_limit: fn() -> bool,
    ) {
        while let Some((id, reward_e8s)) = self.rewards.pop_first() {
            match neuron_store.with_neuron_mut(&id, |neuron| {
                let auto_stake = neuron.auto_stake_maturity.unwrap_or(false);
                if auto_stake {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron
                            .staked_maturity_e8s_equivalent
                            .unwrap_or_default()
                            .saturating_add(reward_e8s),
                    );
                } else {
                    neuron.maturity_e8s_equivalent =
                        neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
                }
            }) {
                Ok(_) => {}
                Err(e) => {
                    println!(
                        "{}Error rewarding neuron {:?} during reward_distribution.\
                    This should not be possible as neuron existence is checked when \
                    rewards are calculated: {}",
                        LOG_PREFIX, id, e
                    );
                }
            };
            if is_over_instructions_limit() {
                break;
            }
        }
    }
```
