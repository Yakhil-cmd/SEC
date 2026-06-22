### Title
Wrong Variable in Error Message Masks Incorrect Validation Logic - (File: rs/sns/governance/src/types.rs)

### Summary

In `validate_neuron_minimum_stake_e8s()` within `NervousSystemParameters`, the error message for the validation failure uses `neuron_minimum_stake_e8s` in both format positions where the second should be `transaction_fee_e8s`. This is a copy-paste bug analogous to the Streamr `onUndelegate()` issue: a validation function compares two different quantities but then reports the wrong value in its diagnostic output, obscuring the actual constraint being enforced.

### Finding Description

In `rs/sns/governance/src/types.rs`, the function `validate_neuron_minimum_stake_e8s` checks that `neuron_minimum_stake_e8s > transaction_fee_e8s`. When the check fails, the error message is:

```rust
if neuron_minimum_stake_e8s <= transaction_fee_e8s {
    Err(format!(
        "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
        NervousSystemParameters.transaction_fee_e8s ({neuron_minimum_stake_e8s})"
    ))
```

The second interpolated value is `{neuron_minimum_stake_e8s}` but it should be `{transaction_fee_e8s}`. The error message therefore prints the same value twice — the minimum stake — instead of showing the actual transaction fee that the minimum stake must exceed. [1](#0-0) 

This function is called as part of `NervousSystemParameters::validate()`, which is invoked during SNS governance parameter updates submitted by any SNS token holder via governance proposals. [2](#0-1) 

### Impact Explanation

The validation logic itself (`neuron_minimum_stake_e8s <= transaction_fee_e8s`) is correct and does enforce the right constraint. However, the error message always displays `neuron_minimum_stake_e8s` for both values. When an SNS operator or governance participant receives this error, they cannot determine from the message alone what the actual `transaction_fee_e8s` value is that they need to exceed. This:

1. Misleads operators into thinking the minimum stake equals the transaction fee (since both printed values are identical).
2. Makes debugging and remediation harder — the operator cannot tell from the error message what value to set `neuron_minimum_stake_e8s` above.
3. In a governance context where proposals are submitted on-chain, a misleading error message can cause repeated failed proposals and wasted governance cycles.

The impact is governance authorization / parameter validation correctness: any unprivileged SNS governance participant who submits a `ManageNervousSystemParameters` proposal with an invalid `neuron_minimum_stake_e8s` will receive a misleading error.

### Likelihood Explanation

This code path is reachable by any SNS neuron holder who submits a governance proposal to update `NervousSystemParameters`. The bug is triggered whenever `neuron_minimum_stake_e8s <= transaction_fee_e8s` is set in a proposal. The condition is realistic — an SNS could have a non-default transaction fee, and a proposal author might accidentally set the minimum stake too low. The bug is present in production code and is not gated behind any privileged access.

### Recommendation

Change the second format argument from `neuron_minimum_stake_e8s` to `transaction_fee_e8s`:

```rust
if neuron_minimum_stake_e8s <= transaction_fee_e8s {
    Err(format!(
        "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
        NervousSystemParameters.transaction_fee_e8s ({transaction_fee_e8s})"
    ))
``` [3](#0-2) 

### Proof of Concept

1. Deploy an SNS with `transaction_fee_e8s = 10_000`.
2. Submit a governance proposal to set `neuron_minimum_stake_e8s = 5_000` (less than the transaction fee).
3. The proposal validation calls `NervousSystemParameters::validate()` → `validate_neuron_minimum_stake_e8s()`.
4. The check `5_000 <= 10_000` is true, so an error is returned.
5. The error message reads: `"NervousSystemParameters.neuron_minimum_stake_e8s (5000) must be greater than NervousSystemParameters.transaction_fee_e8s (5000)"` — both values show `5000` (the minimum stake), not `10000` (the actual transaction fee).
6. The operator cannot determine from this message that the transaction fee is `10_000` and that they need to set the minimum stake above that value. [4](#0-3)

### Citations

**File:** rs/sns/governance/src/types.rs (L571-594)
```rust
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
