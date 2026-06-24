### Title
SNS Voting Rewards Permanently Lost When `total_reward_shares == 0` on Settled Proposals — (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

In SNS governance `distribute_rewards`, when proposals are settled in a reward round but no neuron actually voted (`total_reward_shares == dec!(0)`), the reward event's `end_timestamp_seconds` is still advanced and the proposals are marked as settled. The rollover mechanism only triggers when `settled_proposals` is empty. Because proposals are present and settled, the entire rewards purse for that round is permanently lost rather than carried forward.

---

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` always computes a `rewards_purse_e8s` from the token supply and the configured reward rate, regardless of participation: [1](#0-0) 

It then builds `neuron_id_to_reward_shares` by iterating proposal ballots and counting only neurons whose vote passes `eligible_for_rewards()` (i.e., actually voted Yes or No): [2](#0-1) 

When `total_reward_shares == dec!(0)` — which occurs whenever all eligible neurons abstained — the function skips distribution with only a warning log: [3](#0-2) 

Critically, execution continues unconditionally to update `latest_reward_event`, advancing `end_timestamp_seconds` and recording the settled proposals: [4](#0-3) 

The rollover predicate `rewards_rolled_over()` is defined solely on whether `settled_proposals` is empty: [5](#0-4) 

Because `settled_proposals` is **not** empty (the proposals were just settled), `rewards_rolled_over()` returns `false`, and `e8s_equivalent_to_be_rolled_over()` returns `0`: [6](#0-5) 

Consequently, in the next reward round, `rewards_purse_e8s` starts from `0` rollover instead of carrying the unspent purse forward: [7](#0-6) 

The same structural flaw exists in NNS governance. When `total_voting_rights < 0.001`, `reward_distribution` is set to `None` but the `RewardEvent` is still emitted with `settled_proposals` populated and `day_after_genesis` advanced: [8](#0-7) 

The NNS rollover predicate has the identical definition: [9](#0-8) 

---

### Impact Explanation

For any SNS reward round in which proposals exist but `total_reward_shares == 0`, the entire `rewards_purse_e8s` (a non-zero amount derived from the token supply and reward rate) is silently discarded. The `RewardEvent` records `total_available_e8s_equivalent > 0` alongside `distributed_e8s_equivalent == 0`, but the difference is never rolled over and never distributed. Neuron holders permanently lose the maturity they were entitled to for that round. This is a ledger conservation bug: the governance canister accounts for a reward purse that it then neither distributes nor preserves.

---

### Likelihood Explanation

In SNS, a proposal can only be created when eligible voters exist (the `electoral_roll.is_empty()` guard at line 5287–5292 prevents creation otherwise). However, eligible voters are free to abstain — they receive a ballot with `Vote::Unspecified` and are never forced to cast a vote. If all neurons abstain on every proposal in a given round, `total_reward_shares` is exactly `0`. This is reachable without any privileged access: a low-participation SNS, a contentious proposal that all holders choose to ignore, or a coordinated abstention by a majority of stake are all realistic triggers. The attacker entry path is an ordinary `manage_neuron` call (or simply inaction) by any neuron holder.

---

### Recommendation

The rollover predicate should account for the case where proposals were settled but no rewards were actually distributed. One approach:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
        || self.distributed_e8s_equivalent == 0
}
```

Alternatively, `distribute_rewards` should skip settling proposals and skip advancing `end_timestamp_seconds` when `total_reward_shares == dec!(0)`, treating the round identically to a round with no proposals (pure rollover). The same fix applies to the NNS `calculate_voting_rewards` path when `total_voting_rights < 0.001`.

---

### Proof of Concept

1. Deploy an SNS with `voting_rewards_parameters` configured (non-zero reward rate).
2. Create a proposal. At creation time, eligible neurons exist, so the proposal is accepted.
3. Let the entire voting period elapse without any neuron casting a vote (all abstain).
4. Wait for `distribute_rewards` to be triggered by `run_periodic_tasks`.
5. Observe the emitted `RewardEvent`:
   - `settled_proposals` contains the proposal ID (non-empty).
   - `total_available_e8s_equivalent > 0` (reward purse was computed).
   - `distributed_e8s_equivalent == 0` (nothing distributed).
6. Advance time by one more reward round and trigger `distribute_rewards` again.
7. Observe that the new `rewards_purse_e8s` starts from `0` rollover — the previous purse is gone.
8. No neuron ever receives the maturity that should have been distributed in step 4. [3](#0-2) [4](#0-3) [10](#0-9)

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

**File:** rs/nns/governance/src/governance.rs (L6712-6759)
```rust
        let reward_distribution = if total_voting_rights < 0.001 {
            println!(
                "{}WARNING: total_voting_rights == {}, even though considered_proposals \
                 is nonempty (see earlier log). Therefore, we skip incrementing maturity \
                 to avoid dividing by zero (or super small number).",
                LOG_PREFIX, total_voting_rights,
            );
            None
        } else {
            let mut reward_distribution = RewardsDistribution::new();
            for (neuron_id, used_voting_rights) in voters_to_used_voting_right {
                if self.neuron_store.contains(neuron_id) {
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;

                    reward_distribution.add_reward(neuron_id, reward);

                    // NOTE: This is the only reason we are checking the existence of neurons
                    // at this stage. Otherwise, we could defer until we distribute them in the
                    // schedule task.
                    actually_distributed_e8s_equivalent += reward;
                } else {
                    println!(
                        "{}Cannot find neuron {}, despite having voted with power {} \
                            in the considered reward period. The reward that should have been \
                            distributed to this neuron is simply skipped, so the total amount \
                            of distributed reward for this period will be lower than the maximum \
                            allowed.",
                        LOG_PREFIX, neuron_id.id, used_voting_rights
                    );
                }
            }
            Some(reward_distribution)
        };

        let reward_event = RewardEvent {
            day_after_genesis,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent: actually_distributed_e8s_equivalent,
            total_available_e8s_equivalent: total_available_e8s_equivalent_float as u64,
            rounds_since_last_distribution: Some(rounds_since_last_distribution),
            latest_round_available_e8s_equivalent: Some(
                latest_round_available_e8s_equivalent_float as u64,
            ),
        };

        Some((reward_event, reward_distribution))
```

**File:** rs/nns/governance/src/reward/calculation.rs (L144-147)
```rust
    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```
