### Title
SNS Governance Voting Rewards Permanently Lost When Proposals Settle With Zero Total Voting Power - (`rs/sns/governance/src/governance.rs`)

### Summary

In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function permanently discards the entire accumulated `rewards_purse_e8s` when proposals are settled in a round but no neuron cast an eligible vote (`total_reward_shares == dec!(0)`). The rollover mechanism only triggers when `settled_proposals.is_empty()`, so a round with settled-but-unvoted proposals silently destroys the rewards purse with no recovery path.

### Finding Description

`distribute_rewards` in the SNS governance canister computes a `rewards_purse_e8s` that includes both the current round's newly minted rewards and any amount rolled over from previous empty rounds. [1](#0-0) 

It then tallies voting shares from ballots on settled proposals. Only `Yes` and `No` votes pass the `eligible_for_rewards()` filter; `Unspecified` (abstain) ballots are skipped. [2](#0-1) 

When `total_reward_shares == dec!(0)`, the function logs a warning and distributes nothing (`distributed_e8s_equivalent` stays `0`), but still marks all `considered_proposals` as settled and writes a new `RewardEvent` with those non-empty `settled_proposals`. [3](#0-2) [4](#0-3) 

The rollover logic in `RewardEvent::e8s_equivalent_to_be_rolled_over` only returns `total_available_e8s_equivalent` when `rewards_rolled_over()` is `true`, which requires `settled_proposals.is_empty()`. [5](#0-4) 

Because `settled_proposals` is **not** empty in this scenario, `rewards_rolled_over()` returns `false`, `e8s_equivalent_to_be_rolled_over()` returns `0`, and the next round's `rewards_purse_e8s` starts from zero — the entire purse from the current round is silently discarded.

The NNS governance has the same structural issue in `calculate_voting_rewards` / `distribute_voting_rewards_to_neurons`: [6](#0-5) [7](#0-6) 

### Impact Explanation

SNS governance token maturity rewards are permanently lost. The lost amount equals the full `rewards_purse_e8s` for that round, which can include multiple rounds of accumulated rollover. Maturity is the mechanism by which SNS token holders earn yield for participating in governance; permanent loss of maturity is a direct ledger conservation violation for SNS token holders.

### Likelihood Explanation

This is realistic in:
1. **Early-stage SNS deployments** where neuron participation is low and proposals pass their voting window with all neurons abstaining (following a neuron that never votes, or simply not voting).
2. **Adversarial scenario**: any SNS governance participant can submit a proposal and coordinate (or simply wait) for a round where no neuron casts a `Yes`/`No` vote, triggering the loss. No privileged access is required — only the ability to submit a proposal and the passage of time.

The entry path is fully unprivileged: a canister caller or governance user submits a proposal via the SNS governance canister's public `manage_neuron` endpoint, and the periodic `run_periodic_tasks` heartbeat calls `distribute_rewards` automatically.

### Recommendation

When `total_reward_shares == dec!(0)` but `considered_proposals` is non-empty, the rewards purse should be rolled over to the next round rather than discarded. This can be done by treating the event as a rollover event — either by not recording the proposals as settled until a round where at least one neuron votes, or by explicitly carrying `rewards_purse_e8s` forward in the new `RewardEvent`'s `total_available_e8s_equivalent` and setting `settled_proposals` to empty (so `rewards_rolled_over()` returns `true`).

Alternatively, the `rewards_rolled_over()` predicate should be broadened to also return `true` when `distributed_e8s_equivalent == 0` regardless of whether proposals were settled.

### Proof of Concept

**Scenario**: An SNS is launched. One proposal is created and reaches `ReadyToSettle`. All neurons abstain (or follow a neuron that never votes). The periodic task fires `distribute_rewards`.

1. `considered_proposals` = `[proposal_1]` (non-empty)
2. All ballots have `Vote::Unspecified` → `neuron_id_to_reward_shares` is empty → `total_reward_shares == dec!(0)`
3. Branch at line 5946 is taken: no maturity is added to any neuron; `distributed_e8s_equivalent = 0`
4. `proposal_1` is marked settled; `p.ballots.clear()` is called
5. New `RewardEvent` written: `settled_proposals = [proposal_1]`, `distributed_e8s_equivalent = 0`, `total_available_e8s_equivalent = Some(rewards_purse_e8s)`
6. Next round: `e8s_equivalent_to_be_rolled_over()` checks `rewards_rolled_over()` → `settled_proposals.is_empty()` → `false` → returns `0`
7. Next round's `rewards_purse_e8s` starts from `0` — the entire prior purse is gone permanently [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rs/sns/governance/src/governance.rs (L5892-5934)
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
        // Freeze reward shares, now that we are done adding them up.
        let neuron_id_to_reward_shares = neuron_id_to_reward_shares;
        let total_reward_shares: Decimal = neuron_id_to_reward_shares.values().sum();
```

**File:** rs/sns/governance/src/governance.rs (L5946-5952)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
```

**File:** rs/sns/governance/src/governance.rs (L6083-6092)
```rust
        // Conclude this round of rewards.
        self.proto.latest_reward_event = Some(RewardEvent {
            round: new_reward_event_round,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent,
            end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
            rounds_since_last_distribution: Some(new_rounds_count),
            total_available_e8s_equivalent,
        })
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

**File:** rs/nns/governance/src/governance.rs (L6712-6719)
```rust
        let reward_distribution = if total_voting_rights < 0.001 {
            println!(
                "{}WARNING: total_voting_rights == {}, even though considered_proposals \
                 is nonempty (see earlier log). Therefore, we skip incrementing maturity \
                 to avoid dividing by zero (or super small number).",
                LOG_PREFIX, total_voting_rights,
            );
            None
```

**File:** rs/nns/governance/src/reward/calculation.rs (L120-147)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }

    /// Calculates the rounds_since_last_distribution in this event that should
    /// be "rolled over" into the next `RewardEvent`.
    ///
    /// Behavior:
    /// - If rewards were distributed for this event, then no rounds should be
    ///   rolled over, so this function returns 0.
    /// - Otherwise, this function returns
    ///   `rounds_since_last_distribution`.
    pub(crate) fn rounds_since_last_distribution_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.rounds_since_last_distribution.unwrap_or(0)
        } else {
            0
        }
    }

    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```
