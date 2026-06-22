### Title
SNS Governance Reward Rate Parameters Updated Without Settling Current Reward Period - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The `perform_manage_nervous_system_parameters` function in SNS governance directly overwrites `voting_rewards_parameters` (including `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, and `reward_rate_transition_duration_seconds`) without first distributing rewards for the elapsed period under the old parameters. When `distribute_rewards` subsequently runs, it applies the new rate retroactively to the entire period since the last reward event, causing incorrect neuron maturity distributions for all SNS token holders.

### Finding Description

`perform_manage_nervous_system_parameters` in `rs/sns/governance/src/governance.rs` unconditionally overwrites `self.proto.parameters` with the new parameters: [1](#0-0) 

No call to `distribute_rewards` precedes this write. When `distribute_rewards` later runs (via `run_periodic_tasks`), it reads the **current** (post-update) `voting_rewards_parameters` and applies them to every elapsed round since `latest_reward_event.end_timestamp_seconds`: [2](#0-1) 

The reward purse loop then evaluates `reward_rate_at` and `round_duration()` using the **new** parameters for all elapsed rounds, including those that elapsed before the parameter change: [3](#0-2) 

`reward_rate_at` is a function of `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, and `reward_rate_transition_duration_seconds`: [4](#0-3) 

Because `perform_manage_nervous_system_parameters` does not first settle the current reward period, the new rate is applied retroactively to the entire unsettled window. The proto comment itself acknowledges the danger: `voting_rewards_parameters` "probably should not be changed, because the results would probably be pretty confusing." [5](#0-4) 

The `ManageNervousSystemParameters` action is a standard governance proposal type, executable by any SNS governance majority: [6](#0-5) 

### Impact Explanation

When `initial_reward_rate_basis_points` or `final_reward_rate_basis_points` is changed via a `ManageNervousSystemParameters` proposal, the new rate is applied to the entire elapsed period since the last `RewardEvent`, not just the period after the change. If the rate is decreased, all neurons receive fewer maturity rewards than they legitimately earned during the pre-change window. If the rate is increased, neurons receive excess maturity. Because maturity is the basis for minting SNS tokens (via `disburse_maturity`), this directly affects token supply and neuron holder balances. The impact scales with the length of the unsettled window and the magnitude of the rate change.

### Likelihood Explanation

Any SNS governance majority can submit and pass a `ManageNervousSystemParameters` proposal that modifies `voting_rewards_parameters`. This is a standard, documented governance action — not a malicious exploit. The bug is triggered by a legitimate governance operation that any SNS community might reasonably want to perform (e.g., adjusting inflation rates). The proto comment warning against changing these parameters is not enforced in code, so the bug is reachable in practice.

### Recommendation

In `perform_manage_nervous_system_parameters`, before overwriting `self.proto.parameters`, call `distribute_rewards` (or an equivalent settlement function) to close out the current reward period under the old `voting_rewards_parameters`. This ensures the new rate only applies to rounds that begin after the parameter change, matching the expected semantics.

### Proof of Concept

1. An SNS is initialized with `initial_reward_rate_basis_points = 1000` (10%/year) and `round_duration_seconds = 86400` (1 day). The last `RewardEvent` was at `T = 0`.
2. At `T = 180 days`, a governance majority passes a `ManageNervousSystemParameters` proposal setting `initial_reward_rate_basis_points = 100` (1%/year).
3. `perform_manage_nervous_system_parameters` immediately writes the new parameters without calling `distribute_rewards`.
4. At `T = 181 days`, `distribute_rewards` runs. It computes `new_rounds_count = 181` (all 181 days since `T = 0`) and evaluates `reward_rate_at` using the new 1%/year rate for every round.
5. Neurons receive rewards calculated at 1%/year for the full 181-day window, instead of 10%/year for the first 180 days and 1%/year for the last day — approximately a 10× shortfall in maturity for the pre-change period.

### Citations

**File:** rs/sns/governance/src/governance.rs (L2144-2146)
```rust
            Action::ManageNervousSystemParameters(params) => {
                self.perform_manage_nervous_system_parameters(params)
            }
```

**File:** rs/sns/governance/src/governance.rs (L2595-2598)
```rust
        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L5808-5814)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
```

**File:** rs/sns/governance/src/governance.rs (L5861-5871)
```rust
            for i in 1..=new_rounds_count {
                let seconds_since_genesis = round_duration_seconds
                    .saturating_mul(i)
                    .saturating_add(reward_start_timestamp_seconds)
                    .saturating_sub(self.proto.genesis_timestamp_seconds);

                let current_reward_rate = voting_rewards_parameters.reward_rate_at(
                    crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
                );

                result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
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
