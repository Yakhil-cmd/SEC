### Title
Missing Upper Bound on `max_dissolve_delay_seconds` in SNS `NervousSystemParameters` Allows Indefinite Token Locking - (File: rs/sns/governance/src/types.rs)

### Summary
The SNS governance canister's `NervousSystemParameters` validation for `max_dissolve_delay_seconds` only checks that the field is present (non-`None`), but imposes no upper bound ceiling. A governance majority can pass a `ManageNervousSystemParameters` proposal setting this value to `u64::MAX` (≈ 584 billion years), after which any neuron that increases its dissolve delay to that value will have its staked tokens permanently locked with no mechanism to reduce the delay.

### Finding Description
In `rs/sns/governance/src/types.rs`, the `validate_max_dissolve_delay_seconds` function performs only a presence check:

```rust
fn validate_max_dissolve_delay_seconds(&self) -> Result<u64, String> {
    self.max_dissolve_delay_seconds.ok_or_else(|| {
        "NervousSystemParameters.max_dissolve_delay_seconds must be set".to_string()
    })
}
``` [1](#0-0) 

No ceiling constant exists for `max_dissolve_delay_seconds`, unlike other parameters such as `initial_voting_period_seconds` (bounded by `INITIAL_VOTING_PERIOD_SECONDS_CEILING`), `max_proposals_to_keep_per_action` (bounded by `MAX_PROPOSALS_TO_KEEP_PER_ACTION_CEILING`), `max_followees_per_function` (bounded by `MAX_FOLLOWEES_PER_FUNCTION_CEILING`), and `max_number_of_neurons` (bounded by `MAX_NUMBER_OF_NEURONS_CEILING`): [2](#0-1) 

The `ManageNervousSystemParameters` proposal action calls `new_parameters.inherit_from(current_parameters).validate()` at execution time, which routes through `validate_max_dissolve_delay_seconds`: [3](#0-2) 

The proposal validation path at submission time also calls the same `validate()`: [4](#0-3) 

The `configure_neuron` function uses the live `max_dissolve_delay_seconds` from parameters as the cap when a neuron increases its dissolve delay: [5](#0-4) 

Once a neuron's dissolve delay is set to `u64::MAX`, it cannot be reduced — the SNS governance only allows increasing dissolve delay, never decreasing it. The neuron's staked tokens become permanently locked.

Similarly, `validate_max_neuron_age_for_age_bonus` also has no upper bound: [6](#0-5) 

### Impact Explanation
If `max_dissolve_delay_seconds` is set to `u64::MAX` via a `ManageNervousSystemParameters` proposal:

1. Any neuron that subsequently calls `IncreaseDissolveDelay` or `SetDissolveTimestamp` with the maximum value will have its dissolve delay set to `u64::MAX` seconds (≈ 584 billion years).
2. The SNS neuron's staked tokens become permanently inaccessible — the `Disburse` command is blocked while the neuron is not dissolved, and the dissolve delay cannot be decreased.
3. Even if `max_dissolve_delay_seconds` is later reduced by a subsequent proposal, neurons that already set their delay to `u64::MAX` retain that delay permanently.
4. Voting power calculations using `max_dissolve_delay_seconds` as a divisor remain arithmetically safe (no division by zero since the value is non-zero), but the economic effect of indefinite locking is severe. [7](#0-6) 

### Likelihood Explanation
In early SNS deployments, the founding team's developer neurons frequently hold a majority of voting power. A single actor controlling majority stake can unilaterally pass a `ManageNervousSystemParameters` proposal (even under the critical 67% threshold if they hold sufficient stake). The `ManageNervousSystemParameters` action is reachable by any neuron holder who can submit and pass a proposal — it is a standard, documented governance action with no additional access control beyond voting power. [8](#0-7) 

### Recommendation
Introduce a ceiling constant for `max_dissolve_delay_seconds` analogous to the ceilings already defined for other parameters. A reasonable upper bound would be `8 * ONE_YEAR_SECONDS` (matching the NNS maximum) or a similarly bounded value. The `validate_max_dissolve_delay_seconds` function should be updated to reject values exceeding this ceiling:

```rust
pub const MAX_DISSOLVE_DELAY_SECONDS_CEILING: u64 = 8 * ONE_YEAR_SECONDS;

fn validate_max_dissolve_delay_seconds(&self) -> Result<u64, String> {
    let v = self.max_dissolve_delay_seconds.ok_or_else(|| {
        "NervousSystemParameters.max_dissolve_delay_seconds must be set".to_string()
    })?;
    if v > Self::MAX_DISSOLVE_DELAY_SECONDS_CEILING {
        return Err(format!(
            "NervousSystemParameters.max_dissolve_delay_seconds ({v}) must not exceed {}",
            Self::MAX_DISSOLVE_DELAY_SECONDS_CEILING
        ));
    }
    Ok(v)
}
```

Apply the same pattern to `validate_max_neuron_age_for_age_bonus`.

### Proof of Concept

1. An SNS neuron holder with majority voting power submits a `ManageNervousSystemParameters` proposal:
   ```
   NervousSystemParameters {
       max_dissolve_delay_seconds: Some(u64::MAX),
       ..Default::default()
   }
   ```
2. The proposal passes `validate_and_render_manage_nervous_system_parameters` because `validate_max_dissolve_delay_seconds` only checks `is_some()` — `u64::MAX` passes.
3. `perform_manage_nervous_system_parameters` executes, setting `self.proto.parameters.max_dissolve_delay_seconds = u64::MAX`.
4. Any neuron owner calls `IncreaseDissolveDelay` with `additional_dissolve_delay_seconds = u32::MAX` (the maximum allowed increment per call) repeatedly, or uses `SetDissolveTimestamp` with `dissolve_timestamp_seconds = now + u64::MAX`, capped to `u64::MAX` by the neuron configure logic.
5. The neuron's dissolve delay is now `u64::MAX` seconds. The `Disburse` command is permanently blocked. The staked tokens are irretrievably locked. [9](#0-8) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/types.rs (L379-431)
```rust
    /// This is an upper bound for `max_proposals_to_keep_per_action`. Exceeding it
    /// may cause degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_PROPOSALS_TO_KEEP_PER_ACTION_CEILING: u32 = 700;

    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;

    /// This is an upper bound for `max_number_of_proposals_with_ballots`. Exceeding
    /// it may cause degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING: u64 = 700;

    /// This is an upper bound for `initial_voting_period_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const INITIAL_VOTING_PERIOD_SECONDS_CEILING: u64 = 30 * ONE_DAY_SECONDS;

    /// This is a lower bound for `initial_voting_period_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const INITIAL_VOTING_PERIOD_SECONDS_FLOOR: u64 = ONE_DAY_SECONDS;

    /// This is an upper bound for `wait_for_quiet_deadline_increase_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_CEILING: u64 = 30 * ONE_DAY_SECONDS;

    /// This is a lower bound for `wait_for_quiet_deadline_increase_seconds`. We're setting it to
    /// 1 instead of 0 because values of 0 are not currently well-tested.
    pub const WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_FLOOR: u64 = 1;

    /// This is an upper bound for `max_followees_per_function`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_FOLLOWEES_PER_FUNCTION_CEILING: u64 = 15;

    /// This is an upper bound for `max_number_of_principals_per_neuron`. Exceeding
    /// it may cause may cause degradation in the governance canister or the subnet
    /// hosting the SNS.
    pub const MAX_NUMBER_OF_PRINCIPALS_PER_NEURON_CEILING: u64 = 15;

    /// This is a lower bound for `max_number_of_principals_per_neuron`.
    /// Decreasing it below this number is problematic because SNS Swap assumes
    /// that there are allowed to be at least 5 principals per
    /// neuron during ClaimSwapNeuronsRequest.
    pub const MAX_NUMBER_OF_PRINCIPALS_PER_NEURON_FLOOR: u64 = 5;

    /// This is an upper bound for `max_dissolve_delay_bonus_percentage`. High values
    /// may improve the incentives when voting, but too-high values may also lead
    /// to an over-concentration of voting power. The value used by the NNS is 100.
    pub const MAX_DISSOLVE_DELAY_BONUS_PERCENTAGE_CEILING: u64 = 900;

    /// This is an upper bound for `max_age_bonus_percentage`. High values
    /// may improve the incentives when voting, but too-high values may also lead
    /// to an over-concentration of voting power. The value used by the NNS is 25.
    pub const MAX_AGE_BONUS_PERCENTAGE_CEILING: u64 = 400;
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

**File:** rs/sns/governance/src/types.rs (L791-796)
```rust
    /// Validates that the nervous system parameter max_dissolve_delay_seconds is well-formed.
    fn validate_max_dissolve_delay_seconds(&self) -> Result<u64, String> {
        self.max_dissolve_delay_seconds.ok_or_else(|| {
            "NervousSystemParameters.max_dissolve_delay_seconds must be set".to_string()
        })
    }
```

**File:** rs/sns/governance/src/types.rs (L798-805)
```rust
    /// Validates that the nervous system parameter max_neuron_age_for_age_bonus is well-formed.
    fn validate_max_neuron_age_for_age_bonus(&self) -> Result<(), String> {
        self.max_neuron_age_for_age_bonus.ok_or_else(|| {
            "NervousSystemParameters.max_neuron_age_for_age_bonus must be set".to_string()
        })?;

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

**File:** rs/sns/governance/src/governance.rs (L4185-4199)
```rust
        let max_dissolve_delay_seconds = self
            .proto
            .parameters
            .as_ref()
            .expect("NervousSystemParameters not present")
            .max_dissolve_delay_seconds
            .expect("NervousSystemParameters must have max_dissolve_delay_seconds");

        let neuron = self
            .proto
            .neurons
            .get_mut(&id.to_string())
            .ok_or_else(|| Self::neuron_not_found_error(id))?;

        neuron.configure(now, configure, max_dissolve_delay_seconds)?;
```

**File:** rs/sns/governance/src/proposal.rs (L527-537)
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

```

**File:** rs/sns/governance/src/neuron.rs (L196-220)
```rust
    pub fn voting_power(
        &self,
        now_seconds: u64,
        max_dissolve_delay_seconds: u64,
        max_neuron_age_for_age_bonus: u64,
        max_dissolve_delay_bonus_percentage: u64,
        max_age_bonus_percentage: u64,
    ) -> u64 {
        // We compute the stake adjustments in u128.
        let stake = self.voting_power_stake_e8s() as u128;
        // Dissolve delay is capped to max_dissolve_delay_seconds, but we cap it
        // again here to make sure, e.g., if this changes in the future.
        let d = std::cmp::min(
            self.dissolve_delay_seconds(now_seconds),
            max_dissolve_delay_seconds,
        ) as u128;
        // 'd_stake' is the stake with bonus for dissolve delay.
        let d_stake = stake
            + if max_dissolve_delay_seconds > 0 {
                (stake * d * max_dissolve_delay_bonus_percentage as u128)
                    / (100 * max_dissolve_delay_seconds as u128)
            } else {
                0
            };
        // Sanity check.
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1027-1030)
```rust
        /// Id = 2.
        #[prost(message, tag = "6")]
        ManageNervousSystemParameters(super::NervousSystemParameters),
        /// Upgrade a canister that is controlled by the SNS Governance canister.
```
