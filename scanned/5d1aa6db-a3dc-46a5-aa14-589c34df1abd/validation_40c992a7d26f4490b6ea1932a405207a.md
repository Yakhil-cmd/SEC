### Title
Abrupt `VotingRewardsParameters` Change Without Timelock in SNS Governance - (File: rs/sns/governance/src/governance.rs)

### Summary
`perform_manage_nervous_system_parameters` applies `VotingRewardsParameters` changes atomically upon proposal execution with no timelock, no rate-of-change guard, and no delay beyond the governance voting window. Setting `reward_rate_transition_duration_seconds` to `0` causes `reward_rate_at` to immediately return `final_reward_rate_basis_points` for all future reward rounds, collapsing the reward curve in a single proposal execution. Neuron stakers who locked tokens for years under a published reward schedule have no on-chain mechanism to exit before the change takes effect.

### Finding Description
`VotingRewardsParameters` (`initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, `reward_rate_transition_duration_seconds`) defines the quadratic reward rate curve for an SNS. These fields are changeable via a `ManageNervousSystemParameters` governance proposal.

Upon proposal execution, `perform_manage_nervous_system_parameters` immediately overwrites `self.proto.parameters`:

```rust
// rs/sns/governance/src/governance.rs:2595-2598
match new_params.validate() {
    Ok(()) => {
        self.proto.parameters = Some(new_params);
        Ok(())
    }
```

No delay, no rate-of-change limit, no timelock is applied after the vote passes. [1](#0-0) 

The validation for `reward_rate_transition_duration_seconds` only enforces `0..` (any non-negative value):

```rust
// rs/sns/governance/src/reward.rs:262-268
fn reward_rate_transition_duration_seconds_defects(&self) -> Vec<String> {
    require_field_set_and_in_range(
        "reward_rate_transition_duration_seconds",
        &self.reward_rate_transition_duration_seconds,
        0..,
    )
}
``` [2](#0-1) 

The `reward_rate_at` function immediately returns `final_reward_rate()` when `reward_rate_transition_duration_seconds == 0` or when `time_since_genesis >= reward_rate_transition_duration_seconds`:

```rust
// rs/sns/governance/src/reward.rs:213-216
if reward_rate_transition_duration_seconds == 0
    || time_since_genesis.as_secs() >= i2d(reward_rate_transition_duration_seconds)
{
    return self.final_reward_rate();
}
``` [3](#0-2) 

There is no check on the magnitude of the change from the current values. An SNS governance majority can reduce `initial_reward_rate_basis_points` from 10,000 (100%) to 0, or set `reward_rate_transition_duration_seconds` to 0, in a single proposal execution. The test suite explicitly confirms `reward_rate_transition_duration_seconds: Some(0)` passes validation and causes the reward rate to immediately equal `final_reward_rate_basis_points` for all time: [4](#0-3) 

### Impact Explanation
Neuron stakers who locked tokens with dissolve delays of months or years under a published reward schedule (e.g., `initial_reward_rate_basis_points = 10_000`, `reward_rate_transition_duration_seconds = 252_460_800` — 8 years) can have their expected rewards eliminated in a single proposal execution. Because neurons cannot be dissolved instantly (dissolve delay is enforced), stakers cannot exit before the change takes effect. The reward curve is the primary economic incentive for long-term staking; abrupt collapse of this curve is directly analogous to the `updateA` vulnerability in stable-swap pools, where the amplification parameter shapes the price curve and its abrupt change harms liquidity providers. [5](#0-4) 

### Likelihood Explanation
Medium. `ManageNervousSystemParameters` is a critical proposal requiring 67% of exercised voting power and 20% of total voting power, with a minimum 5-day initial voting period for critical proposals: [6](#0-5) 

However, in many SNS deployments the founding team holds a supermajority of tokens at launch and can pass critical proposals unilaterally. The 5-day window is the only protection, and it is insufficient for stakers with long dissolve delays who cannot exit in time. Furthermore, `initial_voting_period_seconds` itself can be reduced to its floor via a prior `ManageNervousSystemParameters` proposal, compressing the reaction window further. [7](#0-6) 

### Recommendation
Implement a rate-of-change guard inside `perform_manage_nervous_system_parameters` for `VotingRewardsParameters` fields. Specifically:

1. **Limit the magnitude of change per proposal**: e.g., `initial_reward_rate_basis_points` may not decrease by more than X% of its current value in a single proposal.
2. **Enforce a minimum `reward_rate_transition_duration_seconds`**: Prohibit setting it to a value smaller than the current `time_since_genesis`, which would cause an immediate jump to `final_reward_rate`.
3. **Add a post-execution timelock**: After a `VotingRewardsParameters` change proposal passes, apply the new parameters only after a mandatory delay (e.g., 7 days), giving stakers time to react.

### Proof of Concept
1. An SNS is deployed with `initial_reward_rate_basis_points = 10_000` (100%), `final_reward_rate_basis_points = 500` (5%), `reward_rate_transition_duration_seconds = 252_460_800` (8 years). Neuron stakers lock tokens expecting a high initial reward rate.
2. The founding team (holding >67% of exercised voting power) submits a `ManageNervousSystemParameters` proposal:
   ```
   VotingRewardsParameters {
       reward_rate_transition_duration_seconds: Some(0),
       initial_reward_rate_basis_points: Some(0),
       final_reward_rate_basis_points: Some(0),
   }
   ```
3. The proposal passes after the 5-day critical voting period. `perform_manage_nervous_system_parameters` immediately sets `self.proto.parameters = Some(new_params)`.
4. The next call to `reward_rate_at` returns `final_reward_rate_basis_points = 0` for all time. No rewards are distributed to any neuron staker.
5. Stakers with multi-year dissolve delays cannot exit and receive zero rewards for the remainder of their lock-up period. [8](#0-7)

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

**File:** rs/sns/governance/src/reward.rs (L197-217)
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
```

**File:** rs/sns/governance/src/reward.rs (L262-268)
```rust
    fn reward_rate_transition_duration_seconds_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "reward_rate_transition_duration_seconds",
            &self.reward_rate_transition_duration_seconds,
            0..,
        )
    }
```

**File:** rs/sns/governance/src/reward.rs (L635-641)
```rust
    #[test]
    fn test_reward_rate_transition_duration_seconds_validation_accepts_zero() {
        let parameters = VotingRewardsParameters {
            reward_rate_transition_duration_seconds: Some(0),
            ..VOTING_REWARDS_PARAMETERS
        };
        assert_is_ok!(parameters.validate());
```

**File:** rs/sns/governance/src/types.rs (L1832-1848)
```rust
            ProposalCriticality::Critical => {
                let initial_voting_period_seconds =
                    initial_voting_period_seconds.unwrap_or_default();
                let wait_for_quiet_deadline_increase_seconds =
                    wait_for_quiet_deadline_increase_seconds.unwrap_or_default();

                VotingDurationParameters {
                    initial_voting_period: PbDuration {
                        seconds: Some(initial_voting_period_seconds.max(5 * ONE_DAY_SECONDS)),
                    },
                    wait_for_quiet_deadline_increase: PbDuration {
                        seconds: Some(wait_for_quiet_deadline_increase_seconds.max(
                            2 * ONE_DAY_SECONDS + ONE_DAY_SECONDS / 2, // 2.5 days
                        )),
                    },
                }
            }
```

**File:** rs/sns/governance/src/proposal.rs (L527-548)
```rust
/// Validates and renders a proposal with action ManageNervousSystemParameters.
fn validate_and_render_manage_nervous_system_parameters(
    new_parameters: &NervousSystemParameters,
    current_parameters: &NervousSystemParameters,
) -> Result<String, String> {
    if new_parameters == &NervousSystemParameters::default() {
        return Err("NervousSystemParameters: at least one field must be set.".to_string());
    }

    new_parameters.inherit_from(current_parameters).validate()?;

    Ok(format!(
        r"# Proposal to change nervous system parameters:
## Current nervous system parameters:

{:#?}

## New nervous system parameters:

{:#?}",
        &current_parameters, new_parameters
    ))
```
