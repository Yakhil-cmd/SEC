Audit Report

## Title
SNS Governance Accumulated Reward Capture via Stake-Before-Proposal-Creation Front-Running - (`File: rs/sns/governance/src/governance.rs`)

## Summary
SNS governance accumulates voting rewards across rollover rounds (rounds with no settled proposals) and distributes the entire accumulated purse when a proposal eventually settles, using ballot voting power snapshotted at proposal-creation time. An unprivileged attacker can stake a large amount of SNS tokens just before a proposal is created, vote on it, and capture a disproportionate share of the multi-round accumulated rewards purse. NNS governance added a voting-power-spike detection mechanism to mitigate this exact attack class; SNS governance has no equivalent.

## Finding Description
**Reward rollover accumulation:** In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the rewards purse by summing over all elapsed rounds since the last reward event, including any previously rolled-over amount via `e8s_equivalent_to_be_rolled_over()`. [1](#0-0)  The rollover logic is explicit in `rs/sns/governance/src/types.rs`: if a round has no settled proposals, `total_available_e8s_equivalent` is carried forward. [2](#0-1) 

**Ballot voting power fixed at proposal-creation time:** In `compute_ballots_for_new_proposal`, each eligible neuron's current voting power is snapshotted into its ballot at the moment the proposal is created. [3](#0-2)  During `distribute_rewards`, reward shares are computed directly from `ballot.voting_power`, so a neuron staked just before proposal creation receives the same reward weight as one staked for the entire rollover period. [4](#0-3) 

**NNS mitigation absent in SNS:** NNS governance added `VotingPowerSnapshots` with a `MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE = 1.5` guard. [5](#0-4)  The `SnapshotVotingPowerTask` runs daily; if a spike is detected, the previous snapshot's ballots are used instead of current voting power. [6](#0-5)  SNS `run_periodic_tasks` calls `distribute_rewards` directly with no spike guard, and a grep across all SNS source files confirms zero occurrences of any voting power snapshot or spike detection logic. [7](#0-6) 

**Exploit path:**
1. Attacker observes a large `total_available_e8s_equivalent` in `latest_reward_event` (publicly queryable on-chain).
2. Attacker stakes `S` SNS tokens with dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds` (unprivileged `manage_neuron` / `ClaimOrRefresh`).
3. A proposal is created (by anyone, including the attacker). Ballots are created with current voting power: attacker's ballot gets `voting_power = VP_attacker`.
4. Attacker votes on the proposal.
5. Proposal settles → `distribute_rewards` is called. Attacker's reward share = `VP_attacker / (V_existing + VP_attacker) × N × round_reward`, where `N` is the number of rollover rounds.
6. Attacker begins dissolving the neuron immediately after voting.

No existing check prevents this: the only eligibility gate is `dissolve_delay_seconds >= min_dissolve_delay_for_vote`, which the attacker satisfies by construction. [8](#0-7) 

## Impact Explanation
This is a concrete financial attack on SNS governance reward distribution. Long-term stakers who bore the economic risk of the entire rollover period have their reward share diluted by an attacker who was staked only for the proposal's voting period. The attacker captures rewards accumulated during rounds they were not participating in, at the direct expense of legitimate long-term participants. This constitutes significant SNS governance security impact with concrete, quantifiable user harm — fitting the High ($2,000–$10,000) impact class: "Significant SNS or infrastructure security impact with concrete user or protocol harm."

## Likelihood Explanation
- All required information (`latest_reward_event`, `total_available_e8s_equivalent`) is publicly queryable on-chain, enabling precise timing.
- Any unprivileged principal can stake SNS tokens and claim a neuron via `manage_neuron` (`ClaimOrRefresh`).
- The minimum dissolve delay is configurable per SNS and can be short (e.g., days), minimizing the attacker's capital lock-up.
- SNS instances with long periods of low proposal activity (many rollover rounds) are the most profitable targets and are common in practice.
- No privileged access, key compromise, or consensus-level attack is required.
- The attack is repeatable across multiple SNS instances and multiple reward cycles.

## Recommendation
Add a voting-power-spike detection mechanism to SNS governance analogous to the one in NNS (`rs/nns/governance/src/governance/voting_power_snapshots.rs`). Specifically:
1. Periodically snapshot total and per-neuron voting power in SNS governance (e.g., daily, analogous to `SnapshotVotingPowerTask`).
2. When creating ballots for a new proposal in `compute_ballots_for_new_proposal`, check whether the current total voting power exceeds a threshold multiple (e.g., 1.5×) of recent snapshots.
3. If a spike is detected, use the most recent non-spike snapshot's voting power to create ballots instead of the current voting power.

## Proof of Concept
**Deterministic integration test plan:**
1. Initialize an SNS with `neuron_minimum_dissolve_delay_to_vote_seconds = 1 day` and `round_duration = 1 day`.
2. Advance time 30 days with no proposals. Verify `latest_reward_event.total_available_e8s_equivalent` equals 30 × `round_reward`.
3. Create an attacker neuron with stake `S ≈ V_existing` and dissolve delay = 1 day.
4. Create a proposal. Verify attacker's ballot `voting_power ≈ V_existing`.
5. Have attacker vote yes. Advance time past voting period to settle the proposal.
6. Call `distribute_rewards`. Assert attacker's maturity increase ≈ 50% of 30 × `round_reward`.
7. Assert long-term stakers' maturity increase ≈ 50% of 30 × `round_reward` (vs. ~100% without the attack).
8. Repeat with NNS spike detection enabled to confirm the mitigation prevents the disproportionate capture.

### Citations

**File:** rs/sns/governance/src/governance.rs (L5255-5261)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }
```

**File:** rs/sns/governance/src/governance.rs (L5263-5279)
```rust
            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
```

**File:** rs/sns/governance/src/governance.rs (L5503-5521)
```rust
        let should_distribute_rewards = self.should_distribute_rewards();

        // Getting the total governance token supply from the ledger is expensive enough
        // that we don't want to do it on every call to `run_periodic_tasks`. So
        // we only fetch it when it's needed, which is when rewards should be
        // distributed
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }
```

**File:** rs/sns/governance/src/governance.rs (L5854-5875)
```rust
        let rewards_purse_e8s = {
            let mut result = Decimal::from(
                self.latest_reward_event()
                    .e8s_equivalent_to_be_rolled_over(),
            );
            let supply = i2d(supply.get_e8s());

            for i in 1..=new_rounds_count {
                let seconds_since_genesis = round_duration_seconds
                    .saturating_mul(i)
                    .saturating_add(reward_start_timestamp_seconds)
                    .saturating_sub(self.proto.genesis_timestamp_seconds);

                let current_reward_rate = voting_rewards_parameters.reward_rate_at(
                    crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
                );

                result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
            }

            result
        };
```

**File:** rs/sns/governance/src/governance.rs (L5892-5931)
```rust
        // Add up reward shares based on voting power that was exercised.
        let mut neuron_id_to_reward_shares: HashMap<NeuronId, Decimal> = HashMap::new();
        for proposal_id in &considered_proposals {
            if let Some(proposal) = self.get_proposal_data(*proposal_id) {
                for (voter, ballot) in &proposal.ballots {
                    #[allow(clippy::blocks_in_conditions)]
                    if !Vote::try_from(ballot.vote)
                        .unwrap_or_else(|_| {
                            println!(
                                "{}Vote::from invoked with unexpected value {}.",
                                log_prefix(),
                                ballot.vote
                            );
                            Vote::Unspecified
                        })
                        .eligible_for_rewards()
                    {
                        continue;
                    }

                    match NeuronId::from_str(voter) {
                        Ok(neuron_id) => {
                            let reward_shares = i2d(ballot.voting_power);
                            *neuron_id_to_reward_shares
                                .entry(neuron_id)
                                .or_insert_with(|| dec!(0)) += reward_shares;
                        }
                        Err(e) => {
                            log!(
                                ERROR,
                                "Could not use voter {} to calculate total_voting_rights \
                                 since it's NeuronId was invalid. Underlying error: {:?}.",
                                voter,
                                e
                            );
                        }
                    }
                }
            }
        }
```

**File:** rs/sns/governance/src/types.rs (L2054-2067)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
    }

    // Not copied from NNS: fn rounds_since_last_distribution_to_be_rolled_over

    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```

**File:** rs/nns/governance/src/governance/voting_power_snapshots.rs (L17-21)
```rust
const MAX_VOTING_POWER_SNAPSHOTS: u64 = 7;
/// The multiplier used to define what is a "voting power spike": if the current total voting
/// power is more than this multiplier times the minimum total voting power in the snapshots,
/// then we consider it a spike.
const MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE: f64 = 1.5;
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
