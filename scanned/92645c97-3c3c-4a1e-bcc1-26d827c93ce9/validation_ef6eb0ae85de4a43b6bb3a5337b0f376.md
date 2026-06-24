### Title
Reward Rate Parameter Changes Applied Retroactively to Pending Reward Period Without Prior Settlement - (File: rs/sns/governance/src/governance.rs)

### Summary

In SNS Governance, `perform_manage_nervous_system_parameters` updates `VotingRewardsParameters` (including `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, `reward_rate_transition_duration_seconds`, and `round_duration_seconds`) immediately upon proposal execution without first triggering a reward distribution event. The next call to `distribute_rewards` then retroactively applies the new rate parameters to all elapsed rounds since the last reward event, including the period that elapsed under the old parameters. This distorts neuron maturity accrual for all SNS participants.

### Finding Description

`perform_manage_nervous_system_parameters` in `rs/sns/governance/src/governance.rs` directly overwrites `self.proto.parameters` with the new `NervousSystemParameters`: [1](#0-0) 

No reward settlement step precedes this write. The reward distribution logic in `distribute_rewards` reads `voting_rewards_parameters` from the live `self.nervous_system_parameters_or_panic()` at distribution time: [2](#0-1) 

It then iterates over all rounds elapsed since the last reward event and applies `reward_rate_at` using the **current** (post-change) parameters for every round, including rounds that elapsed before the parameter change: [3](#0-2) 

`reward_rate_at` uses `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, and `reward_rate_transition_duration_seconds` to compute the rate: [4](#0-3) 

Similarly, `round_duration_seconds` is used to compute `new_rounds_count` and `reward_event_end_timestamp_seconds`, so changing it also retroactively alters how many rounds are counted for the elapsed period: [5](#0-4) 

The `ManageNervousSystemParameters` proposal action is the governance-accessible entry point: [6](#0-5) 

`VotingRewardsParameters` is a mutable sub-field of `NervousSystemParameters`: [7](#0-6) 

### Impact Explanation

Any SNS governance proposal that changes `VotingRewardsParameters` causes the pending reward period (time elapsed since the last `RewardEvent`) to be recalculated under the new parameters. If the new rate is lower, neuron holders who voted during the pre-change period receive less maturity than they earned. If the new rate is higher, they receive a windfall. The magnitude of distortion scales with the length of the pending period (up to one full `round_duration_seconds`). Since maturity is the mechanism by which neuron holders earn staking rewards and eventually convert to tokens, this directly affects the economic fairness of the SNS reward system. [8](#0-7) 

### Likelihood Explanation

Medium. Any SNS governance proposal to adjust reward rates — a routine governance action — triggers this distortion. The SNS governance system is designed to allow token holders to change `VotingRewardsParameters` via `ManageNervousSystemParameters` proposals. The distortion is bounded by the elapsed time since the last reward event (at most one `round_duration_seconds`), but for SNS instances with long round durations (up to `MAX_REWARD_ROUND_DURATION_SECONDS`), the effect can be significant. No privileged access is required beyond passing a governance proposal. [9](#0-8) 

### Recommendation

Before applying new `VotingRewardsParameters` in `perform_manage_nervous_system_parameters`, the governance canister should force a reward settlement for the elapsed period under the old parameters. Because `distribute_rewards` requires an async ledger call to fetch total token supply, one approach is to snapshot the old parameters alongside a timestamp and use them for rounds that ended before the parameter change took effect. Alternatively, the proposal execution could be structured to trigger a reward distribution as a prerequisite step, similar to how the original `set_interest_fee_bps` and `set_pair_jump_interest_rate_model` functions in the referenced report were expected to call accrual functions before modifying protocol parameters.

### Proof of Concept

1. SNS is initialized with `initial_reward_rate_basis_points = 1000` (10%/year) and `round_duration_seconds = 604800` (7 days).
2. A reward event occurs at `T=0`. No further reward events occur.
3. At `T = 6 days`, a `ManageNervousSystemParameters` proposal executes, setting `initial_reward_rate_basis_points = 100` (1%/year) and `final_reward_rate_basis_points = 50` (0.5%/year).
4. `perform_manage_nervous_system_parameters` writes the new parameters immediately with no prior `distribute_rewards` call.
5. At `T = 7 days`, `run_periodic_tasks` calls `should_distribute_rewards` → `true`, then calls `distribute_rewards`.
6. `distribute_rewards` reads the new `voting_rewards_parameters` (1%/year rate), computes `new_rounds_count = 1`, and calculates the reward purse for the entire 7-day period at the new 1%/year rate.
7. Neuron holders who voted during days 0–6 (when the rate was 10%/year) receive rewards calculated at 1%/year — a 10× reduction in maturity for that period.

The relevant code path: [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2579-2617)
```rust
    /// Executes a ManageNervousSystemParameters proposal by updating Governance's
    /// NervousSystemParameters
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

**File:** rs/sns/governance/src/governance.rs (L5725-5753)
```rust
    fn should_distribute_rewards(&self) -> bool {
        let now = self.env.now();

        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            None => return false,
            Some(ok) => ok,
        };
        let seconds_since_last_reward_event = now.saturating_sub(
            self.latest_reward_event()
                .end_timestamp_seconds
                .unwrap_or_default(),
        );

        let round_duration_seconds = match voting_rewards_parameters.round_duration_seconds {
            Some(s) => s,
            None => {
                log!(
                    ERROR,
                    "round_duration_seconds unset:\n{:#?}",
                    voting_rewards_parameters,
                );
                return false;
            }
        };

        seconds_since_last_reward_event > round_duration_seconds
```

**File:** rs/sns/governance/src/governance.rs (L5769-5782)
```rust
        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            Some(voting_rewards_parameters) => voting_rewards_parameters,
            None => {
                log!(
                    ERROR,
                    "distribute_rewards called even though \
                     voting_rewards_parameters not set.",
                );
                return;
            }
        };
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

**File:** rs/sns/governance/src/reward.rs (L254-260)
```rust
    fn round_duration_seconds_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "round_duration_seconds",
            &self.round_duration_seconds,
            1..=*MAX_REWARD_ROUND_DURATION_SECONDS,
        )
    }
```

**File:** rs/sns/governance/src/reward.rs (L314-328)
```rust
    /// Any empty fields of `self` are overwritten with the corresponding fields of `base`.
    pub fn inherit_from(&self, base: &Self) -> Self {
        Self {
            round_duration_seconds: self.round_duration_seconds.or(base.round_duration_seconds),
            reward_rate_transition_duration_seconds: self
                .reward_rate_transition_duration_seconds
                .or(base.reward_rate_transition_duration_seconds),
            initial_reward_rate_basis_points: self
                .initial_reward_rate_basis_points
                .or(base.initial_reward_rate_basis_points),
            final_reward_rate_basis_points: self
                .final_reward_rate_basis_points
                .or(base.final_reward_rate_basis_points),
        }
    }
```
