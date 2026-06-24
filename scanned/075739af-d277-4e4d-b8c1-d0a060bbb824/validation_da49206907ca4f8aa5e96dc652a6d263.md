### Title
Voting Rewards Not Settled Before `VotingRewardsParameters` Change in SNS Governance - (`File: rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance canister's `perform_manage_nervous_system_parameters` function applies new `VotingRewardsParameters` (including `round_duration_seconds`, `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, and `reward_rate_transition_duration_seconds`) immediately without first calling `distribute_rewards`. This causes the new reward parameters to apply retroactively from the last reward event, producing incorrect reward accounting for all SNS neuron holders.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `perform_manage_nervous_system_parameters` is the execution handler for `ManageNervousSystemParameters` proposals. It directly overwrites `self.proto.parameters` with the new parameters:

```rust
// rs/sns/governance/src/governance.rs:2581-2617
fn perform_manage_nervous_system_parameters(
    &mut self,
    proposed_params: NervousSystemParameters,
) -> Result<(), GovernanceError> {
    let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());
    // ...
    match new_params.validate() {
        Ok(()) => {
            self.proto.parameters = Some(new_params); // ← applied immediately, no distribute_rewards call
            Ok(())
        }
        // ...
    }
}
```

The `distribute_rewards` function (line 5763) uses `voting_rewards_parameters` in two critical ways:

1. **Round count calculation** — it divides elapsed time since the last reward event by `round_duration_seconds` to determine how many reward rounds have passed:
   ```rust
   let new_rounds_count = now
       .saturating_sub(reward_start_timestamp_seconds)
       .saturating_div(round_duration_seconds);
   ```

2. **Reward rate calculation** — for each round it calls `voting_rewards_parameters.reward_rate_at(...)` using `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, and `reward_rate_transition_duration_seconds`.

When `voting_rewards_parameters` is changed mid-period (between two reward events), the next `distribute_rewards` call uses the new parameters to retroactively recalculate the entire elapsed period since `latest_reward_event.end_timestamp_seconds`, not just the time after the change.

The proto definition itself acknowledges the danger:
```
// rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs:1753-1757
/// When this field is not populated, voting rewards are "disabled". Once this
/// is set, it probably should not be changed, because the results would
/// probably be pretty confusing.
```

### Impact Explanation

- **Decreasing `round_duration_seconds`**: The next `distribute_rewards` call counts more rounds for the elapsed period, inflating the reward purse and over-distributing tokens to neuron holders — effectively minting excess governance tokens.
- **Increasing `round_duration_seconds`**: Fewer rounds are counted, under-distributing rewards and causing neuron holders to lose yield they had already accrued.
- **Changing `initial_reward_rate_basis_points` or `final_reward_rate_basis_points`**: The new rate is applied retroactively to all rounds since the last reward event, not just future rounds. This can silently inflate or deflate the reward purse.
- **Changing `reward_rate_transition_duration_seconds`**: Alters the quadratic decay curve retroactively, shifting the effective reward rate for the entire unsettled period.

The impact is a gain or loss of yield for all SNS neuron holders, and potential token supply inflation, with no on-chain signal that the accounting is incorrect.

### Likelihood Explanation

Any SNS community that passes a `ManageNervousSystemParameters` proposal touching `voting_rewards_parameters` triggers this bug — even with entirely benign intent (e.g., adjusting the round duration to improve UX). The entry path is a standard governance proposal submitted by any neuron holder with sufficient voting power. No privileged key or malicious majority is required; a legitimate majority vote is sufficient to trigger the miscalculation.

### Recommendation

`perform_manage_nervous_system_parameters` should call `distribute_rewards` (or an equivalent settlement step) before applying any change to `voting_rewards_parameters`. This ensures the reward purse is settled under the old parameters up to the current moment, and only future rounds use the new parameters:

```rust
fn perform_manage_nervous_system_parameters(
    &mut self,
    proposed_params: NervousSystemParameters,
) -> Result<(), GovernanceError> {
    let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

    // Settle rewards under current parameters before changing them
    if new_params.voting_rewards_parameters
        != self.nervous_system_parameters_or_panic().voting_rewards_parameters
    {
        if self.should_distribute_rewards() {
            // Trigger reward distribution with current supply snapshot
            // (requires supply to be available or cached)
            self.distribute_rewards(current_supply);
        }
    }

    match new_params.validate() {
        Ok(()) => { self.proto.parameters = Some(new_params); Ok(()) }
        Err(msg) => Err(...)
    }
}
```

Alternatively, the governance canister can record the `latest_reward_event.end_timestamp_seconds` at the time of the parameter change and use it as the new baseline for future reward calculations, so that elapsed time before the change is accounted for under the old parameters.

### Proof of Concept

1. An SNS is initialized with `round_duration_seconds = 86400` (1 day) and `initial_reward_rate_basis_points = 250`.
2. 12 hours pass. No reward event fires (only 0.5 rounds elapsed).
3. A `ManageNervousSystemParameters` proposal is passed that sets `round_duration_seconds = 43200` (12 hours).
4. `perform_manage_nervous_system_parameters` immediately writes the new parameters with no `distribute_rewards` call.
5. The next `distribute_rewards` call computes: `new_rounds_count = 43200 / 43200 = 1` round. But the actual elapsed time (12 hours) was accrued under the old 1-day round definition. The reward purse is now calculated as if a full 12-hour round completed, using the new rate — whereas under the old parameters, 0 rounds would have completed and rewards would have rolled over.
6. Neuron holders receive rewards for a period that was not yet due under the original parameters, and future rounds are now shorter, compounding the discrepancy.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2581-2617)
```rust
    fn perform_manage_nervous_system_parameters(
        &mut self,
        proposed_params: NervousSystemParameters,
    ) -> Result<(), GovernanceError> {
        // Only set `self.proto.parameters` if "applying" the proposed params to the
        // current params results in valid params
        let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

        log!(
            INFO,
            "Setting Governance nervous system params to: {:?}",
            &new_params
        );

        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
            }

            // Even though proposals are validated when they are first made, this is still
            // possible, because the inner value of a ManageNervousSystemParameters
            // proposal is only valid with respect to the current
            // nervous_system_parameters() at the time when the proposal was first
            // made. If nervous_system_parameters() changed (by another proposal) since
            // the current proposal was first made, the current proposal might have become
            // invalid. Basically, this might occur if there are conflicting (concurrent)
            // proposals, but we expect this to be highly unusual in practice.
            Err(msg) => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to perform ManageNervousSystemParameters action, proposed \
                        parameters would lead to invalid NervousSystemParameters: {msg}"
                ),
            )),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L5808-5875)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
        if new_rounds_count == 0 {
            // This may happen, in case consider_distributing_rewards was called
            // several times at almost the same time. This is
            // harmless, just abandon.
            return;
        }

        let considered_proposals: Vec<ProposalId> =
            self.ready_to_be_settled_proposal_ids().collect();
        // RewardEvents are generated every time. If there are no proposals to reward, the rewards
        // purse is rolled over via the total_available_e8s_equivalent field.

        // Log if we are about to "backfill" rounds that were missed.
        if new_rounds_count > 1 {
            log!(
                INFO,
                "Some reward distribution should have happened, but were missed. \
                 It is now {}. Whereas, latest_reward_event:\n{:#?}",
                now,
                self.latest_reward_event(),
            );
        }
        let reward_event_end_timestamp_seconds = new_rounds_count
            .saturating_mul(round_duration_seconds)
            .saturating_add(reward_start_timestamp_seconds);

        // What's going on here looks a little complex, but it's just a slightly
        // more advanced version of simple (i.e. non-compounding) interest. The
        // main embellishment is because we are calculating the reward purse
        // over possibly more than one reward round. The possibility of multiple
        // rounds is why we loop over rounds. Otherwise, it boils down to the
        // simple interest formula:
        //
        //   principal * rate * duration
        //
        // Here, the entire token supply is used as the "principal", and the
        // length of a reward round is used as the duration. The reward rate
        // varies from round to round, and is calculated using
        // VotingRewardsParameters::reward_rate_at.
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

**File:** rs/sns/governance/src/reward.rs (L197-241)
```rust
    pub fn reward_rate_at(&self, now: Instant) -> RewardRate {
        let reward_rate_transition_duration_seconds = self
            .reward_rate_transition_duration_seconds
            .expect("reward_rate_transition_duration_seconds unset");

        let time_since_genesis = {
            let result = now - *GENESIS;
            // For the purposes of determining reward rate, treat times before
            // genesis the same as at genesis. This is not expected to occur in
            // practice. This code is just being extra defensive.
            if result.as_secs() < i2d(0) {
                Duration { days: i2d(0) }
            } else {
                result
            }
        };
        if reward_rate_transition_duration_seconds == 0
            || time_since_genesis.as_secs() >= i2d(reward_rate_transition_duration_seconds)
        {
            return self.final_reward_rate();
        }

        // s linearly varies from 1 -> 0 as seconds_since_genesis varies from 0
        // to reward_rate_transition_duration_seconds.
        let transition = LinearMap::new(
            dec!(0)..i2d(reward_rate_transition_duration_seconds),
            dec!(1)..dec!(0),
        );
        let s = transition.apply(time_since_genesis.as_secs());
        // s2 varies quadratically from 1 -> 0 (again, as seconds_since_genesis
        // varies from 0 to reward_rate_transition_duration_seconds), and
        // flattens out as seconds_since_genesis approaches
        // reward_rate_transition_duration_seconds.
        let s2 = s * s;

        // This looks backwards, but we think of variable rate as being added to
        // final growth rate, not initial, and the amount to add is up to
        // initial - final (where initial is thought of as being greater than
        // final).
        let dr = self.initial_reward_rate() - self.final_reward_rate();
        // variable_reward_rate varies from dr to 0 as round varies from
        // 1 to transition_round_count.
        let variable_reward_rate = s2 * dr;

        self.final_reward_rate() + variable_reward_rate
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1753-1757)
```rust
    /// When this field is not populated, voting rewards are "disabled". Once this
    /// is set, it probably should not be changed, because the results would
    /// probably be pretty confusing.
    #[prost(message, optional, tag = "19")]
    pub voting_rewards_parameters: ::core::option::Option<VotingRewardsParameters>,
```
