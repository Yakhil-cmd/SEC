### Title
Unbounded `max_dissolve_delay_seconds` in SNS `NervousSystemParameters` Allows Governance to Permanently Brick Itself - (File: `rs/sns/governance/src/types.rs`)

---

### Summary

The SNS `NervousSystemParameters` validation explicitly documents that bounds exist on parameters to prevent the governance canister from becoming "stuck." However, `max_dissolve_delay_seconds` has **no upper-bound ceiling check** in its validator, unlike every other bounded parameter. A `ManageNervousSystemParameters` proposal can set it to `u64::MAX`, which then unlocks setting `neuron_minimum_dissolve_delay_to_vote_seconds` to `u64::MAX` as well — permanently disenfranchising all neurons and bricking SNS governance with no recovery path.

---

### Finding Description

`NervousSystemParameters` defines ceiling constants and enforces them for all time/count parameters:

- `INITIAL_VOTING_PERIOD_SECONDS_CEILING` = 30 days
- `WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_CEILING` = 30 days
- `MAX_NUMBER_OF_NEURONS_CEILING` = 200,000
- `MAX_PROPOSALS_TO_KEEP_PER_ACTION_CEILING` = 700
- `MAX_FOLLOWEES_PER_FUNCTION_CEILING` = 15
- `MAX_DISSOLVE_DELAY_BONUS_PERCENTAGE_CEILING` = 900 [1](#0-0) 

The code comment explicitly states the motivation:

> "to prevent that the nervous system accidentally chooses parameters that result in an non-upgradable (and thus stuck) governance canister" [2](#0-1) 

However, `validate_max_dissolve_delay_seconds` only checks that the field is `Some` — **no ceiling is enforced**:

```rust
fn validate_max_dissolve_delay_seconds(&self) -> Result<u64, String> {
    self.max_dissolve_delay_seconds.ok_or_else(|| {
        "NervousSystemParameters.max_dissolve_delay_seconds must be set".to_string()
    })
}
``` [3](#0-2) 

The `neuron_minimum_dissolve_delay_to_vote_seconds` validator only checks that it is `<= max_dissolve_delay_seconds`:

```rust
if neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds {
    Err(...)
}
``` [4](#0-3) 

This means: if `max_dissolve_delay_seconds` is set to `u64::MAX`, a subsequent proposal can set `neuron_minimum_dissolve_delay_to_vote_seconds` to `u64::MAX` and pass validation. Since neurons can only increase their dissolve delay up to `max_dissolve_delay_seconds`, and no neuron can ever hold a dissolve delay of `u64::MAX` seconds (~584 billion years), **no neuron can ever vote again**.

The execution path for `ManageNervousSystemParameters` proposals confirms the validation is the only guard: [5](#0-4) 

Proposal submission also validates against current parameters: [6](#0-5) 

---

### Impact Explanation

An SNS governance community that passes two sequential `ManageNervousSystemParameters` proposals:
1. Set `max_dissolve_delay_seconds = u64::MAX`
2. Set `neuron_minimum_dissolve_delay_to_vote_seconds = u64::MAX`

...permanently bricks the SNS. No neuron can ever achieve a dissolve delay of `u64::MAX` seconds, so no neuron can ever vote. No further governance proposals can pass. The SNS governance canister becomes permanently non-upgradable and non-configurable — the exact scenario the bounds system was designed to prevent.

The default value for `max_dissolve_delay_seconds` is `8 * ONE_YEAR_SECONDS` (~252 million seconds): [7](#0-6) 

There is no code path that prevents it from being raised to `u64::MAX`.

---

### Likelihood Explanation

This requires a `ManageNervousSystemParameters` proposal to pass, which requires an SNS governance majority. However:

- The SNS governance design explicitly intends to prevent this class of issue via parameter bounds — the missing ceiling is an inconsistency in the protection model, not an intentional design choice.
- An SNS with a concentrated token distribution (e.g., early-stage SNS, developer-controlled majority) could have a single actor capable of passing such proposals.
- The attack can be executed in two sequential proposals, each of which appears individually reasonable (e.g., "extend max dissolve delay to allow longer staking").
- There is no recovery mechanism once both proposals execute.

---

### Recommendation

Add a `MAX_DISSOLVE_DELAY_SECONDS_CEILING` constant (e.g., `8 * ONE_YEAR_SECONDS` or a reasonable multiple) and enforce it in `validate_max_dissolve_delay_seconds`:

```rust
pub const MAX_DISSOLVE_DELAY_SECONDS_CEILING: u64 = 8 * ONE_YEAR_SECONDS;

fn validate_max_dissolve_delay_seconds(&self) -> Result<u64, String> {
    let max = self.max_dissolve_delay_seconds.ok_or_else(|| {
        "NervousSystemParameters.max_dissolve_delay_seconds must be set".to_string()
    })?;
    if max > Self::MAX_DISSOLVE_DELAY_SECONDS_CEILING {
        return Err(format!(
            "NervousSystemParameters.max_dissolve_delay_seconds ({max}) must be \
             less than or equal to {}",
            Self::MAX_DISSOLVE_DELAY_SECONDS_CEILING
        ));
    }
    Ok(max)
}
```

This is consistent with how all other bounded parameters are handled. [8](#0-7) 

---

### Proof of Concept

1. Deploy an SNS with default parameters (`max_dissolve_delay_seconds = 8 years`, `neuron_minimum_dissolve_delay_to_vote_seconds = 6 months`).
2. Obtain a governance majority (e.g., hold >50% of voting power).
3. Submit `ManageNervousSystemParameters { max_dissolve_delay_seconds: Some(u64::MAX), ..Default::default() }`. This passes `validate_max_dissolve_delay_seconds` (only checks `is_some()`). Proposal executes.
4. Submit `ManageNervousSystemParameters { neuron_minimum_dissolve_delay_to_vote_seconds: Some(u64::MAX), ..Default::default() }`. This passes `validate_neuron_minimum_dissolve_delay_to_vote_seconds` (only checks `<= max_dissolve_delay_seconds`, which is now `u64::MAX`). Proposal executes.
5. All existing neurons have dissolve delays far below `u64::MAX`. No neuron can vote. No proposal can ever pass. SNS governance is permanently bricked. [9](#0-8) [3](#0-2)

### Citations

**File:** rs/sns/governance/src/types.rs (L374-378)
```rust
/// Some constants that define upper bound (ceiling) and lower bounds (floor) for some of
/// the nervous system parameters as well as the default values for the nervous system
/// parameters (until we initialize them). We can't implement Default since it conflicts
/// with PB's.
impl NervousSystemParameters {
```

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

**File:** rs/sns/governance/src/types.rs (L481-481)
```rust
            max_dissolve_delay_seconds: Some(8 * ONE_YEAR_SECONDS), // 8y
```

**File:** rs/sns/governance/src/types.rs (L653-673)
```rust
    /// Validates that the nervous system parameter initial_voting_period_seconds is well-formed.
    fn validate_initial_voting_period_seconds(&self) -> Result<(), String> {
        let initial_voting_period_seconds =
            self.initial_voting_period_seconds.ok_or_else(|| {
                "NervousSystemParameters.initial_voting_period_seconds must be set".to_string()
            })?;

        if initial_voting_period_seconds < Self::INITIAL_VOTING_PERIOD_SECONDS_FLOOR {
            Err(format!(
                "NervousSystemParameters.initial_voting_period_seconds must be greater than {}",
                Self::INITIAL_VOTING_PERIOD_SECONDS_FLOOR
            ))
        } else if initial_voting_period_seconds > Self::INITIAL_VOTING_PERIOD_SECONDS_CEILING {
            Err(format!(
                "NervousSystemParameters.initial_voting_period_seconds must be less than {}",
                Self::INITIAL_VOTING_PERIOD_SECONDS_CEILING
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L752-772)
```rust
    /// Validates that the nervous system parameter
    /// neuron_minimum_dissolve_delay_to_vote_seconds is well-formed.
    fn validate_neuron_minimum_dissolve_delay_to_vote_seconds(&self) -> Result<(), String> {
        let max_dissolve_delay_seconds = self.validate_max_dissolve_delay_seconds()?;

        let neuron_minimum_dissolve_delay_to_vote_seconds = self
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .ok_or_else(|| {
                "NervousSystemParameters.neuron_minimum_dissolve_delay_to_vote_seconds must be set"
                    .to_string()
            })?;

        if neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds {
            Err(format!(
                "The minimum dissolve delay to vote ({neuron_minimum_dissolve_delay_to_vote_seconds}) cannot be greater than the max \
                dissolve delay ({max_dissolve_delay_seconds})"
            ))
        } else {
            Ok(())
        }
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

**File:** rs/sns/governance/src/governance.rs (L2579-2616)
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
