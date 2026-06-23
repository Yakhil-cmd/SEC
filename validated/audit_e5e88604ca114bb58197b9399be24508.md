### Title
Missing Upper-Bound Constraint on `reward_rate_transition_duration_seconds` Allows Permanent Inflation of Initial Reward Rate - (File: rs/sns/governance/src/reward.rs)

### Summary

The `VotingRewardsParameters::reward_rate_transition_duration_seconds_defects()` validation function in the SNS governance canister accepts any value in the range `0..` (i.e., zero to `u64::MAX`) with no upper bound. An SNS deployer or a `ManageNervousSystemParameters` governance proposal can set `reward_rate_transition_duration_seconds` to an astronomically large value (e.g., `u64::MAX ≈ 1.8 × 10¹⁹` seconds, or ~585 billion years). Because the reward rate transition logic in `reward_rate_at()` only returns `final_reward_rate` once `time_since_genesis >= reward_rate_transition_duration_seconds`, a misconfigured huge value means the SNS will permanently pay out at the elevated `initial_reward_rate` rather than transitioning to the lower `final_reward_rate`, causing unbounded token inflation for the entire lifetime of the SNS.

### Finding Description

In `rs/sns/governance/src/reward.rs`, the validation range for `reward_rate_transition_duration_seconds` is `0..` — an unbounded open range:

```rust
fn reward_rate_transition_duration_seconds_defects(&self) -> Vec<String> {
    require_field_set_and_in_range(
        "reward_rate_transition_duration_seconds",
        &self.reward_rate_transition_duration_seconds,
        0..,   // ← no upper bound
    )
}
``` [1](#0-0) 

By contrast, `round_duration_seconds` is bounded by `1..=*MAX_REWARD_ROUND_DURATION_SECONDS`: [2](#0-1) 

The `reward_rate_at()` function only transitions to `final_reward_rate` when `time_since_genesis >= reward_rate_transition_duration_seconds`:

```rust
if reward_rate_transition_duration_seconds == 0
    || time_since_genesis.as_secs() >= i2d(reward_rate_transition_duration_seconds)
{
    return self.final_reward_rate();
}
``` [3](#0-2) 

If `reward_rate_transition_duration_seconds` is set to `u64::MAX` (~585 billion years), the condition `time_since_genesis >= reward_rate_transition_duration_seconds` will never be true in any realistic timeframe, so the SNS will permanently distribute rewards at `initial_reward_rate` instead of transitioning to `final_reward_rate`.

The same missing upper-bound constraint exists in `SnsInitPayload::validate_reward_rate_transition_duration_seconds()` in `rs/sns/init/src/lib.rs`, which only checks that the field is present, not that it is within a sensible range:

```rust
fn validate_reward_rate_transition_duration_seconds(&self) -> Result<(), String> {
    let _reward_rate_transition_duration_seconds = self
        .reward_rate_transition_duration_seconds
        .ok_or("Error: reward_rate_transition_duration_seconds must be specified")?;
    Ok(())
}
``` [4](#0-3) 

This is the SNS init-time validation path called from both `validate_pre_execution()` and `validate_post_execution()`: [5](#0-4) 

The `ManageNervousSystemParameters` proposal path also calls `NervousSystemParameters::validate()`, which calls `validate_voting_rewards_parameters()`, which calls `VotingRewardsParameters::validate()` — the same unbounded check: [6](#0-5) [7](#0-6) 

### Impact Explanation

An SNS configured with `reward_rate_transition_duration_seconds = u64::MAX` will permanently pay out at `initial_reward_rate` (up to 100% per year, the `INITIAL_REWARD_RATE_BASIS_POINTS_CEILING`). This causes:

1. **Unbounded token inflation**: The SNS token supply inflates at the initial (higher) rate indefinitely, diluting all token holders who are not actively staking and voting.
2. **Economic misconfiguration that cannot be easily corrected**: While a `ManageNervousSystemParameters` proposal could fix it, the damage (excess minted tokens) is irreversible.
3. **Governance manipulation**: If `initial_reward_rate` is set to the maximum (100%), an attacker who controls the SNS deployment could configure this to rapidly inflate supply and dilute other token holders.

The impact is analogous to the Zerem `unlockPeriodSec` being set to a huge value — in both cases, a time-based parameter controlling a financial mechanism is unconstrained, leading to permanent misconfiguration of fund flows.

### Likelihood Explanation

**Medium.** This requires either:
- An SNS deployer making a mistake or fat-fingering the `reward_rate_transition_duration_seconds` field during SNS initialization (e.g., accidentally entering a Unix timestamp like `1700000000` instead of a duration in seconds, which is ~53 years — still very long), or
- A malicious SNS deployer intentionally setting this to `u64::MAX` to permanently inflate the token supply.

The `ManageNervousSystemParameters` proposal path is also reachable by any SNS token holder with sufficient voting power, making this exploitable post-deployment as well. The NNS does validate this proposal before execution, but the validation only checks `0..` with no ceiling.

### Recommendation

Add a sensible upper-bound ceiling constant for `reward_rate_transition_duration_seconds` in `VotingRewardsParameters`, analogous to `MAX_REWARD_ROUND_DURATION_SECONDS` for `round_duration_seconds`. A reasonable ceiling would be on the order of 10–50 years (e.g., `50 * ONE_YEAR_SECONDS`). Apply this ceiling in:

1. `VotingRewardsParameters::reward_rate_transition_duration_seconds_defects()` in `rs/sns/governance/src/reward.rs`
2. `SnsInitPayload::validate_reward_rate_transition_duration_seconds()` in `rs/sns/init/src/lib.rs`

### Proof of Concept

1. Deploy an SNS with `SnsInitPayload` containing:
   ```
   reward_rate_transition_duration_seconds = u64::MAX  // ~585 billion years
   initial_reward_rate_basis_points = 10_000           // 100% per year
   final_reward_rate_basis_points = 0                  // 0% per year
   ```
2. Both `validate_pre_execution()` and `validate_post_execution()` pass because `validate_reward_rate_transition_duration_seconds()` only checks `is_some()`, and `VotingRewardsParameters::validate()` accepts `0..` with no ceiling.
3. The SNS governance canister is initialized with these parameters.
4. `reward_rate_at(now)` is called during every reward event. Since `time_since_genesis.as_secs() < i2d(u64::MAX)` will always be true for any realistic timestamp, the function never returns `final_reward_rate()` and always returns the initial 100% rate.
5. The SNS token supply inflates at 100% per year indefinitely, diluting all non-staking token holders.

The same misconfiguration can be introduced post-deployment via a `ManageNervousSystemParameters` proposal, since `validate_and_render_manage_nervous_system_parameters()` calls `new_parameters.inherit_from(current_parameters).validate()`, which uses the same unbounded `0..` check. [8](#0-7)

### Citations

**File:** rs/sns/governance/src/reward.rs (L213-217)
```rust
        if reward_rate_transition_duration_seconds == 0
            || time_since_genesis.as_secs() >= i2d(reward_rate_transition_duration_seconds)
        {
            return self.final_reward_rate();
        }
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

**File:** rs/sns/init/src/lib.rs (L866-867)
```rust
            self.validate_final_reward_rate_basis_points(),
            self.validate_reward_rate_transition_duration_seconds(),
```

**File:** rs/sns/init/src/lib.rs (L1207-1212)
```rust
    fn validate_reward_rate_transition_duration_seconds(&self) -> Result<(), String> {
        let _reward_rate_transition_duration_seconds = self
            .reward_rate_transition_duration_seconds
            .ok_or("Error: reward_rate_transition_duration_seconds must be specified")?;
        Ok(())
    }
```

**File:** rs/sns/governance/src/types.rs (L570-594)
```rust
    /// This validates that the `NervousSystemParameters` are well-formed.
    pub fn validate(&self) -> Result<(), String> {
        self.validate_reject_cost_e8s()?;
        self.validate_neuron_minimum_stake_e8s()?;
        self.validate_transaction_fee_e8s()?;
        self.validate_max_proposals_to_keep_per_action()?;
        self.validate_initial_voting_period_seconds()?;
        self.validate_wait_for_quiet_deadline_increase_seconds()?;
        self.validate_default_followees()?;
        self.validate_max_number_of_neurons()?;
        self.validate_neuron_minimum_dissolve_delay_to_vote_seconds()?;
        self.validate_max_followees_per_function()?;
        self.validate_max_dissolve_delay_seconds()?;
        self.validate_max_neuron_age_for_age_bonus()?;
        self.validate_max_number_of_proposals_with_ballots()?;
        self.validate_neuron_claimer_permissions()?;
        self.validate_neuron_grantable_permissions()?;
        self.validate_max_number_of_principals_per_neuron()?;
        self.validate_voting_rewards_parameters()?;
        self.validate_max_dissolve_delay_bonus_percentage()?;
        self.validate_max_age_bonus_percentage()?;
        self.validate_additional_critical_native_action_ids()?;

        Ok(())
    }
```

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
