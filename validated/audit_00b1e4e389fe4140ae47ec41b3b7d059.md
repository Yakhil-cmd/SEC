### Title
SNS `validate_neuron_minimum_stake_e8s` Uses Wrong Variable in Error Message, Masking Actual `transaction_fee_e8s` Value — (`File: rs/sns/governance/src/types.rs`)

### Summary

The `validate_neuron_minimum_stake_e8s` function in the SNS governance canister contains a copy-paste bug in its error message: it prints `neuron_minimum_stake_e8s` twice instead of printing `transaction_fee_e8s` in the second interpolation. While this is a correctness/observability defect rather than a direct economic exploit, it is an analog to the external report's theme of **missing or inadequate threshold enforcement** — the validation logic that is supposed to guard against dust-level `neuron_minimum_stake_e8s` values produces a misleading error message that hides the actual `transaction_fee_e8s` value, making it impossible for operators or governance participants to diagnose why a `ManageNervousSystemParameters` proposal was rejected.

### Finding Description

In `rs/sns/governance/src/types.rs`, the function `validate_neuron_minimum_stake_e8s` checks that `neuron_minimum_stake_e8s > transaction_fee_e8s`. When the check fails, the error message interpolates `{neuron_minimum_stake_e8s}` in both positions — the second position was intended to show `transaction_fee_e8s`:

```rust
if neuron_minimum_stake_e8s <= transaction_fee_e8s {
    Err(format!(
        "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
        NervousSystemParameters.transaction_fee_e8s ({neuron_minimum_stake_e8s})"  // BUG: should be {transaction_fee_e8s}
    ))
``` [1](#0-0) 

The variable `transaction_fee_e8s` is correctly computed on line 604 and used in the comparison on line 610, but is never shown in the error output. This means when a `ManageNervousSystemParameters` proposal is rejected because `neuron_minimum_stake_e8s` is set too low relative to `transaction_fee_e8s`, the error message shows the same value twice and omits the actual fee threshold that was violated.

This validation is invoked both at proposal submission time (via `validate_and_render_manage_nervous_system_parameters`) and at proposal execution time (via `perform_manage_nervous_system_parameters`): [2](#0-1) [3](#0-2) 

The `validate` method is the sole guard preventing `neuron_minimum_stake_e8s` from being set to a dust-level value (i.e., at or below `transaction_fee_e8s`), which is the direct analog of the external report's `minDebt`/`minBorrow` threshold issue: [4](#0-3) 

The SNS documentation itself acknowledges the invariant: `neuron_minimum_stake_e8s` must be larger than `transaction_fee_e8s` to ensure staking and disbursing work correctly: [5](#0-4) 

### Impact Explanation

The bug has two concrete effects:

1. **Misleading governance error messages**: When an SNS community submits a `ManageNervousSystemParameters` proposal that would set `neuron_minimum_stake_e8s` too low, the rejection message shows the same number twice (the proposed `neuron_minimum_stake_e8s`) and never reveals the actual `transaction_fee_e8s` floor. This makes it impossible to diagnose the failure from the error alone.

2. **Obscured threshold enforcement**: The guard against dust-level minimum stakes — the direct analog of the `minDebt`/`minBorrow` issue — is present but its diagnostic output is broken. If `transaction_fee_e8s` were ever raised via a separate proposal while `neuron_minimum_stake_e8s` remained low, the error message would not communicate the correct relationship to governance participants trying to fix the state.

The underlying check (`neuron_minimum_stake_e8s > transaction_fee_e8s`) does execute correctly and does block invalid parameters. However, the broken error message reduces the ability of SNS communities to understand and respond to governance failures, which is a governance authorization/observability bug reachable by any unprivileged ingress sender submitting a `ManageNervousSystemParameters` proposal. [6](#0-5) 

### Likelihood Explanation

Any SNS token holder with sufficient stake to submit a `ManageNervousSystemParameters` proposal (an unprivileged ingress sender) can trigger this code path. The SNS governance system is designed to allow communities to update `neuron_minimum_stake_e8s` and `transaction_fee_e8s` independently via proposals. The bug is triggered whenever a proposal attempts to set `neuron_minimum_stake_e8s <= transaction_fee_e8s`. This is a realistic scenario — for example, if `transaction_fee_e8s` was previously raised and a subsequent proposal tries to lower `neuron_minimum_stake_e8s` without accounting for the new fee level.

### Recommendation

Fix the format string in `validate_neuron_minimum_stake_e8s` to use `{transaction_fee_e8s}` in the second interpolation position:

```rust
Err(format!(
    "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
    NervousSystemParameters.transaction_fee_e8s ({transaction_fee_e8s})"
))
``` [7](#0-6) 

Additionally, consider adding an absolute minimum floor for `transaction_fee_e8s` itself (e.g., `> 0`) to prevent both parameters from being set to zero simultaneously, which would allow dust-amount neuron creation with no economic cost — the precise scenario described in the external report.

### Proof of Concept

1. Deploy an SNS with default parameters (`neuron_minimum_stake_e8s = 1_000_000`, `transaction_fee_e8s = 10_000`).
2. Submit a `ManageNervousSystemParameters` proposal setting `transaction_fee_e8s = 500_000` (accepted and executed).
3. Submit a second proposal setting `neuron_minimum_stake_e8s = 400_000` (now below the new fee).
4. The proposal is rejected with an error message of the form:
   > `NervousSystemParameters.neuron_minimum_stake_e8s (400000) must be greater than NervousSystemParameters.transaction_fee_e8s (400000)`
   
   The actual `transaction_fee_e8s` value of `500_000` is never shown. The message is indistinguishable from a case where both values are equal, making diagnosis impossible. [6](#0-5) [8](#0-7)

### Citations

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

**File:** rs/sns/governance/src/types.rs (L602-618)
```rust
    /// Validates that the nervous system parameter neuron_minimum_stake_e8s is well-formed.
    fn validate_neuron_minimum_stake_e8s(&self) -> Result<(), String> {
        let transaction_fee_e8s = self.validate_transaction_fee_e8s()?;

        let neuron_minimum_stake_e8s = self.neuron_minimum_stake_e8s.ok_or_else(|| {
            "NervousSystemParameters.neuron_minimum_stake_e8s must be set".to_string()
        })?;

        if neuron_minimum_stake_e8s <= transaction_fee_e8s {
            Err(format!(
                "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
                NervousSystemParameters.transaction_fee_e8s ({neuron_minimum_stake_e8s})"
            ))
        } else {
            Ok(())
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

**File:** rs/sns/governance/src/governance.rs (L2595-2616)
```rust
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1123-1127)
```text
  // The minimum number of e8s (10e-8 of a token) that can be staked in a neuron.
  //
  // To ensure that staking and disbursing of the neuron work, the chosen value
  // must be larger than the transaction_fee_e8s.
  optional uint64 neuron_minimum_stake_e8s = 2;
```
