### Title
Rolled-Over Voting Rewards Can Be Claimed by Newly Staked Neurons - (File: rs/sns/governance/src/governance.rs)

### Summary
In both SNS and NNS governance, when reward rounds pass without any proposals to settle, the rewards purse rolls over and accumulates. A new neuron staked just before a proposal is created can claim a proportional share of **all** accumulated rolled-over rewards, including those from periods before the neuron existed. This is a direct analog of the Uniswap LimitOrderHook "accrued fees can be stolen" vulnerability.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function computes `rewards_purse_e8s` by starting with the rolled-over balance from the previous `RewardEvent` and adding the current round's newly minted rewards:

```rust
let rewards_purse_e8s = {
    let mut result = Decimal::from(
        self.latest_reward_event()
            .e8s_equivalent_to_be_rolled_over(),  // ← includes ALL prior rolled-over rounds
    );
    ...
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
    result
};
``` [1](#0-0) 

The `e8s_equivalent_to_be_rolled_over()` helper returns the full `total_available_e8s_equivalent` whenever `settled_proposals.is_empty()`, meaning every round with no proposals adds to a growing pot: [2](#0-1) 

When a proposal finally settles, reward shares are computed purely from the voting power exercised on that proposal's ballots — with no reference to when each neuron was staked:

```rust
for (voter, ballot) in &proposal.ballots {
    ...
    let reward_shares = i2d(ballot.voting_power);
    *neuron_id_to_reward_shares.entry(neuron_id).or_insert_with(|| dec!(0)) += reward_shares;
}
``` [3](#0-2) 

Each neuron's payout is then:

```rust
let neuron_reward_e8s = rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);
``` [4](#0-3) 

There is no per-neuron snapshot of the rewards purse at the time of staking. A neuron staked one block before a proposal is created receives the same proportional share of the entire accumulated purse as a neuron that has been staked for years.

The identical pattern exists in NNS governance: [5](#0-4) [6](#0-5) 

### Impact Explanation
An attacker who observes that an SNS has accumulated a large rolled-over rewards purse (many rounds with no proposals) can:

1. Stake a large quantity of SNS tokens just before submitting or voting on a proposal, acquiring, say, 90% of total voting power.
2. Vote on the proposal (or submit one if `neuron_minimum_dissolve_delay_to_vote_seconds` is 0 or very small).
3. Receive ~90% of the entire accumulated rewards purse — including all rewards rolled over from rounds before the attacker ever held a neuron.
4. Immediately begin dissolving (if dissolve delay is 0).

Existing long-term stakers who were present during all the rollover rounds receive only their diluted proportional share of the remaining ~10%. Rewards that were "owed" to the existing community are siphoned to a just-in-time entrant. This is a direct governance-level accounting bug: the rewards ledger does not conserve the entitlement of prior participants.

### Likelihood Explanation
**SNS (higher likelihood):** `neuron_minimum_dissolve_delay_to_vote_seconds` is a configurable SNS parameter with no enforced minimum floor beyond being ≤ `max_dissolve_delay_seconds`. [7](#0-6) 

Many deployed SNS instances set this to a short duration. Any SNS that experiences a period of low proposal activity (common in early-stage or dormant SNS projects) will accumulate a large rolled-over purse. The attack requires only a standard `manage_neuron` stake call followed by a vote — both are unprivileged ingress messages.

**NNS (lower likelihood):** NNS enforces a ~6-month minimum dissolve delay, which raises the capital lock-up cost. NNS also has near-daily proposals, so rollover accumulation is limited. The attack is theoretically possible but economically unattractive under normal conditions.

### Recommendation
Maintain a per-neuron snapshot of the rewards purse at the time the neuron's ballot is cast (or at neuron creation). When distributing, each neuron should only be eligible for the portion of `rewards_purse_e8s` that accrued **after** their ballot was recorded. Alternatively, track a `reward_purse_at_stake_time` field per neuron and subtract it from the current purse when computing that neuron's eligible share. This mirrors the "per-user fee snapshot" approach recommended in the external report.

### Proof of Concept
Assume an SNS with:
- Total existing staked supply: 1,000,000 SNS tokens (all held by long-term stakers)
- 10 rounds have passed with no proposals → rolled-over purse = 10,000 SNS tokens
- `neuron_minimum_dissolve_delay_to_vote_seconds` = 0

Attack steps (all via standard `manage_neuron` ingress calls):
1. Attacker stakes 9,000,000 SNS tokens (90% of new total supply of 10,000,000).
2. Attacker submits a Motion proposal; their neuron gets a ballot with 90% of total voting power.
3. Attacker votes Yes; proposal settles at end of round.
4. `distribute_rewards` runs: `rewards_purse_e8s` = 10,000 (rolled-over) + ~1,000 (current round) ≈ 11,000 SNS.
5. Attacker's `neuron_reward_shares / total_reward_shares` ≈ 90% → attacker receives ~9,900 SNS in maturity.
6. Long-term stakers collectively receive ~1,100 SNS despite having been staked for all 10 rollover rounds.
7. Attacker immediately sets dissolve delay to 0 and begins dissolving.

The attacker captured ~9,000 SNS tokens of rewards that were accumulated before their participation, at the expense of the existing community. [1](#0-0) [3](#0-2) [2](#0-1)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L5974-5975)
```rust
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);
```

**File:** rs/sns/governance/src/types.rs (L2054-2060)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
    }
```

**File:** rs/nns/governance/src/governance.rs (L6651-6654)
```rust
        let rolling_over_from_previous_reward_event_e8s_equivalent =
            latest_reward_event.e8s_equivalent_to_be_rolled_over();
        let total_available_e8s_equivalent_float = (supply.get_e8s() as f64) * fraction
            + rolling_over_from_previous_reward_event_e8s_equivalent as f64;
```

**File:** rs/nns/governance/src/governance.rs (L6722-6725)
```rust
            for (neuron_id, used_voting_rights) in voters_to_used_voting_right {
                if self.neuron_store.contains(neuron_id) {
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1182-1185)
```text
  // The minimum dissolve delay a neuron must have to be eligible to vote.
  //
  // The chosen value must be smaller than max_dissolve_delay_seconds.
  optional uint64 neuron_minimum_dissolve_delay_to_vote_seconds = 8;
```
