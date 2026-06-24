### Title
SNS Voting Rewards Permanently Lost When Proposals Settle in a Zero-Participation Period - (`File: rs/sns/governance/src/governance.rs`, `rs/sns/governance/src/types.rs`)

---

### Summary

In the SNS governance canister, when proposals are settled in a reward period where no neurons voted (`total_reward_shares == 0`), the rewards purse for that period is neither distributed to neurons nor rolled over to the next period. The rollover recovery mechanism (`rewards_rolled_over()`) uses `settled_proposals.is_empty()` as its sole condition, so a period with settled-but-unvoted proposals is treated as a "distributed" period even though `distributed_e8s_equivalent == 0`. The rewards are permanently lost.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function computes a `rewards_purse_e8s` and then attempts to distribute it proportionally to neurons that voted. When `total_reward_shares == dec!(0)` (no neurons voted on any settled proposal), the distribution is skipped with a log warning: [1](#0-0) 

Despite skipping distribution, the function still constructs a `RewardEvent` with `settled_proposals` set to the non-empty list of proposals that were processed: [2](#0-1) 

The rollover recovery mechanism in `rs/sns/governance/src/types.rs` determines whether to carry the reward purse forward by checking only whether `settled_proposals` is empty: [3](#0-2) 

Because `settled_proposals` is non-empty, `rewards_rolled_over()` returns `false`, and `e8s_equivalent_to_be_rolled_over()` returns `0`: [4](#0-3) 

The next reward period therefore starts with a `rewards_purse_e8s` that does not include the lost amount. The rewards from the zero-participation period are permanently unrecoverable.

The NNS governance has an analogous guard at `total_voting_rights < 0.001` with the same structural flaw: [5](#0-4) 

And the same rollover condition: [6](#0-5) 

---

### Impact Explanation

Governance token maturity (the SNS equivalent of staking rewards) that should have been distributed to voting neurons is permanently destroyed. The lost amount equals `rewards_purse_e8s` for the affected period, which is proportional to the total SNS token supply multiplied by the configured reward rate. For a newly launched SNS with a non-trivial token supply and reward rate, this can represent a material loss of governance incentives. The proposals involved are also permanently marked `Settled` with `reward_event_round` set, so they can never be reconsidered for rewards. [7](#0-6) 

---

### Likelihood Explanation

The trigger condition — proposals settling in a period where `total_reward_shares == 0` — is realistic for a newly launched SNS:

1. An SNS is deployed and its first proposals are submitted (e.g., initial configuration proposals).
2. Neuron holders have not yet set up following relationships or are unaware of the proposals.
3. The proposals' voting period expires with zero votes cast.
4. `distribute_rewards` fires at the end of the reward round, finds `total_reward_shares == 0`, skips distribution, but marks the proposals as settled.
5. The reward purse for that round is lost.

Any SNS governance participant (an unprivileged ingress sender) can trigger this by submitting a proposal and allowing it to expire without votes. The condition is especially likely in the first reward round of a new SNS, directly mirroring the "first period" scenario in the external report.

---

### Recommendation

Change the rollover condition to check whether rewards were actually distributed, not merely whether proposals were settled. Specifically, `rewards_rolled_over()` should return `true` when `distributed_e8s_equivalent == 0` (regardless of whether `settled_proposals` is empty), so that any undistributed reward purse is always carried forward:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.distributed_e8s_equivalent == 0
}
```

This mirrors the fix recommended in the external report: make the recovery path independent of the zero-participation condition.

---

### Proof of Concept

1. Deploy a new SNS with `voting_rewards_parameters` configured (non-zero reward rate).
2. Submit a governance proposal via an unprivileged ingress call.
3. Allow the proposal's voting period to expire without any neuron casting a vote (no `cast_vote` calls).
4. The proposal transitions to `ReadyToSettle`.
5. Wait for `run_periodic_tasks` to call `distribute_rewards` after the reward round ends.
6. Observe: `distribute_rewards` computes a non-zero `rewards_purse_e8s`, enters the `total_reward_shares == dec!(0)` branch, skips neuron maturity updates, but records a `RewardEvent` with `settled_proposals` non-empty and `distributed_e8s_equivalent == 0`.
7. Observe: `e8s_equivalent_to_be_rolled_over()` returns `0` because `rewards_rolled_over()` returns `false`.
8. The next reward period's purse does not include the lost amount. The maturity is permanently gone. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5854-5876)
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
        debug_assert!(rewards_purse_e8s >= dec!(0), "{}", rewards_purse_e8s);
```

**File:** rs/sns/governance/src/governance.rs (L5946-5998)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
        } else {
            for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
                let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) {
                    Ok(neuron) => neuron,
                    Err(err) => {
                        log!(
                            ERROR,
                            "Cannot find neuron {}, despite having voted with power {} \
                             in the considered reward period. The reward that should have been \
                             distributed to this neuron is simply skipped, so the total amount \
                             of distributed reward for this period will be lower than the maximum \
                             allowed. Underlying error: {:?}.",
                            neuron_id,
                            neuron_reward_shares,
                            err
                        );
                        continue;
                    }
                };

                // Dividing before multiplying maximizes our chances of success.
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);

                // Round down, and convert to u64.
                let neuron_reward_e8s = u64::try_from(neuron_reward_e8s).unwrap_or_else(|err| {
                    panic!(
                        "Calculating reward for neuron {neuron_id:?}:\n\
                             neuron_reward_shares: {neuron_reward_shares}\n\
                             rewards_purse_e8s: {rewards_purse_e8s}\n\
                             total_reward_shares: {total_reward_shares}\n\
                             err: {err}",
                    )
                });
                // If the neuron has auto-stake-maturity on, add the new maturity to the
                // staked maturity, otherwise add it to the un-staked maturity.
                if neuron.auto_stake_maturity.unwrap_or(false) {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s,
                    );
                } else {
                    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
                }
                distributed_e8s_equivalent += neuron_reward_e8s;
            }
        }
```

**File:** rs/sns/governance/src/governance.rs (L5999-6030)
```rust
        // Freeze distributed_e8s_equivalent, now that we are done handing out rewards.
        let distributed_e8s_equivalent = distributed_e8s_equivalent;
        // Because we used floor to round rewards to integers (and everything is
        // non-negative), it should be that the amount distributed is not more
        // than the original purse.
        debug_assert!(
            i2d(distributed_e8s_equivalent) <= rewards_purse_e8s,
            "rewards distributed ({distributed_e8s_equivalent}) > purse ({rewards_purse_e8s})",
        );

        // This field is deprecated. People should really use end_timestamp_seconds
        // instead. This value can still be used if round duration is not changed.
        let new_reward_event_round = self.latest_reward_event().round + new_rounds_count;
        // Settle proposals.
        for pid in &considered_proposals {
            // Before considering a proposal for reward, it must be fully processed --
            // because we're about to clear the ballots, so no further processing will be
            // possible.
            self.process_proposal(pid.id);

            let p = match self.get_proposal_data_mut(*pid) {
                Some(p) => p,
                None => {
                    log!(
                        ERROR,
                        "Cannot find proposal {}, despite it being considered for rewards distribution.",
                        pid.id
                    );
                    debug_assert!(
                        false,
                        "It appears that proposal {} has been deleted out from under us \
                         while we were distributing rewards. This should never happen. \
```

**File:** rs/sns/governance/src/types.rs (L2045-2067)
```rust
impl RewardEvent {
    /// Calculates the total_available_e8s_equivalent in this event that should
    /// be "rolled over" into the next `RewardEvent`.
    ///
    /// Behavior:
    /// - If rewards were distributed for this event, then no available_icp_e8s
    ///   should be rolled over, so this function returns 0.
    /// - Otherwise, this function returns
    ///   `total_available_e8s_equivalent`.
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

**File:** rs/nns/governance/src/reward/calculation.rs (L144-147)
```rust
    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```
