### Title
SNS Governance Accumulated Reward Capture via Stake-Before-Proposal-Creation Front-Running - (`File: rs/sns/governance/src/governance.rs`)

### Summary

SNS governance accumulates voting rewards across "rollover rounds" (rounds with no settled proposals). When a proposal is eventually settled, all accumulated rewards are distributed at once to neurons that voted on it, using voting power snapshotted at proposal-creation time. An unprivileged attacker can stake a large amount just before a proposal is created, vote on it, and capture a disproportionate share of the multi-round accumulated rewards purse — while only bearing the risk of the dissolve-delay period. NNS governance added a voting-power-spike detection mechanism to mitigate this exact class of attack; SNS governance has not.

---

### Finding Description

**Reward rollover accumulation (SNS)**

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the rewards purse by summing over all elapsed rounds since the last reward event, including any previously rolled-over amount:

```rust
let rewards_purse_e8s = {
    let mut result = Decimal::from(
        self.latest_reward_event().e8s_equivalent_to_be_rolled_over(),
    );
    for i in 1..=new_rounds_count {
        result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
    }
    result
};
``` [1](#0-0) 

The rollover logic is explicit: if a round has no settled proposals, `total_available_e8s_equivalent` is carried forward into the next event:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent.unwrap_or_default()
    } else { 0 }
}
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
``` [2](#0-1) 

**Reward shares are based on ballot voting power set at proposal-creation time**

Reward shares are computed from `ballot.voting_power`, which is fixed when the proposal is created in `compute_ballots_for_new_proposal`:

```rust
let mut neuron_id_to_reward_shares: HashMap<NeuronId, Decimal> = HashMap::new();
for proposal_id in &considered_proposals {
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots {
            if !Vote::try_from(ballot.vote)...eligible_for_rewards() { continue; }
            let reward_shares = i2d(ballot.voting_power);
            *neuron_id_to_reward_shares.entry(neuron_id).or_insert_with(|| dec!(0)) += reward_shares;
        }
    }
}
``` [3](#0-2) 

Ballots are created with the neuron's current voting power at proposal-creation time:

```rust
let voting_power = v.voting_power(now_seconds, max_dissolve_delay, max_age_bonus, ...);
electoral_roll.insert(k.clone(), Ballot { vote: Vote::Unspecified as i32, voting_power, ... });
``` [4](#0-3) 

**NNS has a mitigation; SNS does not**

NNS governance added a voting-power-spike detection mechanism that uses a previous daily snapshot of voting power to create ballots when a spike (≥1.5× the minimum snapshot total) is detected:

```rust
const MULTIPLIER_THRESHOLD_FOR_VOTING_POWER_SPIKE: f64 = 1.5;
const MAX_VOTING_POWER_SNAPSHOTS: u64 = 7;
``` [5](#0-4) 

The `SnapshotVotingPowerTask` runs daily and records snapshots; if a spike is detected, the previous snapshot's ballots are used instead of current voting power. [6](#0-5) 

SNS governance has no equivalent mechanism. Its `run_periodic_tasks` calls `distribute_rewards` directly with no spike guard: [7](#0-6) 

---

### Impact Explanation

An attacker who stakes a large amount of SNS tokens just before a proposal is created receives a ballot with large voting power. When the proposal settles, the attacker's share of the rewards purse — which may include many rounds of rolled-over rewards — is proportional to their ballot voting power relative to total ballot voting power. The attacker captures rewards accumulated during rounds they were not staked, at the expense of long-term stakers who bore the risk of the entire rollover period. The attacker's only risk is the dissolve-delay period (the minimum required to vote), which is configurable and can be short.

---

### Likelihood Explanation

- The accumulated rewards purse and the `latest_reward_event` are publicly queryable on-chain, so an attacker can precisely time their stake.
- Any unprivileged principal can stake SNS tokens and claim a neuron via `manage_neuron` (`ClaimOrRefresh`).
- The attacker only needs to hold the stake for the proposal's voting period (days to weeks), not the entire rollover period.
- SNS instances with long periods of low proposal activity (many rollover rounds) are the most profitable targets.
- No privileged access, key compromise, or consensus-level attack is required.

---

### Recommendation

Add a voting-power-spike detection mechanism to SNS governance analogous to the one in NNS (`rs/nns/governance/src/governance/voting_power_snapshots.rs`). Specifically:

1. Periodically snapshot total and per-neuron voting power in SNS governance.
2. When creating ballots for a new proposal, check whether the current total voting power exceeds a threshold multiple of recent snapshots.
3. If a spike is detected, use the most recent non-spike snapshot's voting power to create ballots instead of the current voting power.

This prevents an attacker from inflating their ballot share by staking just before a proposal is created.

---

### Proof of Concept

**Setup:** An SNS has been running for 30 rounds with no proposals. The rewards purse has accumulated 30 × `round_reward` tokens. Existing stakers hold a combined voting power of `V_existing`.

**Attack:**
1. Attacker observes the large `total_available_e8s_equivalent` in `latest_reward_event` (publicly queryable).
2. Attacker stakes `S` tokens with dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`, giving voting power `VP_attacker`.
3. A proposal is created (by anyone, including the attacker). Ballots are created with current voting power: attacker gets `ballot.voting_power = VP_attacker`.
4. Attacker votes on the proposal.
5. Proposal settles → `distribute_rewards` is called. Attacker's reward share = `VP_attacker / (V_existing + VP_attacker) × 30 × round_reward`.
6. If `VP_attacker ≈ V_existing`, attacker captures ~50% of 30 rounds of accumulated rewards while only being staked for the proposal's voting period.
7. Attacker begins dissolving the neuron immediately after voting.

Long-term stakers who bore the risk of 30 rounds receive only ~50% of the rewards they would have received without the attack.

### Citations

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

**File:** rs/nns/governance/src/timer_tasks/snapshot_voting_power.rs (L30-58)
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
    }
```
