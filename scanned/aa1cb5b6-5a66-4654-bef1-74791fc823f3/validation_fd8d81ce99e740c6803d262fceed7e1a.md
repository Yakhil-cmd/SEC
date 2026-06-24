### Title
Missing Minimum Floor on `neuron_minimum_dissolve_delay_to_vote_seconds` Collapses SNS Governance Staking Protection - (File: `rs/sns/governance/src/types.rs`)

### Summary
The SNS `NervousSystemParameters.neuron_minimum_dissolve_delay_to_vote_seconds` field has no minimum floor enforced during validation. A `ManageNervousSystemParameters` governance proposal can legally set it to `0`, allowing fully dissolved neurons (zero dissolve delay) to vote. This collapses the staking-based fraud-protection mechanism that is the direct IC analog of the `delayBlocks` guard in the external report.

### Finding Description
`validate_neuron_minimum_dissolve_delay_to_vote_seconds` in `rs/sns/governance/src/types.rs` enforces only one constraint: the value must not exceed `max_dissolve_delay_seconds`. There is no lower-bound (floor) constant analogous to `INITIAL_VOTING_PERIOD_SECONDS_FLOOR` or `WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_FLOOR`.

```rust
// rs/sns/governance/src/types.rs  lines 752-772
fn validate_neuron_minimum_dissolve_delay_to_vote_seconds(&self) -> Result<(), String> {
    let max_dissolve_delay_seconds = self.validate_max_dissolve_delay_seconds()?;
    let neuron_minimum_dissolve_delay_to_vote_seconds = self
        .neuron_minimum_dissolve_delay_to_vote_seconds
        .ok_or_else(|| { ... })?;

    if neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds {
        Err(...)
    } else {
        Ok(())   // ← 0 passes here
    }
}
``` [1](#0-0) 

The same absence of a floor exists in the SNS init-time validator: [2](#0-1) 

By contrast, every other timing-sensitive parameter has an explicit floor constant: [3](#0-2) 

The execution path that applies a `ManageNervousSystemParameters` proposal calls `new_params.validate()` and then unconditionally stores the result: [4](#0-3) 

Because `validate()` calls `validate_neuron_minimum_dissolve_delay_to_vote_seconds` and that function accepts `0`, a proposal setting the field to `0` will pass validation and be committed to state. [5](#0-4) 

### Impact Explanation
`neuron_minimum_dissolve_delay_to_vote_seconds` is the SNS equivalent of `delayBlocks`: it is the sole on-chain guard that forces governance participants to have skin-in-the-game by locking tokens for a minimum period before they may vote. Setting it to `0` means:

- Any principal can stake tokens, vote on a proposal (including treasury-draining `TransferSnsTreasuryFunds` or `UpgradeSnsControlledCanister` proposals), and immediately dissolve and withdraw.
- The economic deterrent against governance attacks is eliminated.
- All future proposals are decided by transient token holders with no long-term alignment, making the SNS governance equivalent to a flash-loan attack surface.

This is a **governance authorization bug** with direct financial impact on every SNS deployed on the IC.

### Likelihood Explanation
The entry path is a standard ingress call: any principal holding an SNS neuron with sufficient voting power (or following relationships that aggregate to a majority) can submit a `ManageNervousSystemParameters` proposal. No privileged key, no admin role, and no subnet-majority is required — only the normal SNS governance quorum. The absence of a floor constant means there is no on-chain safeguard to catch an accidental or malicious zero value, mirroring exactly the `setDelayBlocks` scenario in the external report. SNS communities routinely tune `NervousSystemParameters` via governance proposals, making an accidental misconfiguration realistic.

### Recommendation
Add a `NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_FLOOR` constant (e.g., `ONE_DAY_SECONDS` or a value matching the minimum meaningful staking period) and enforce it inside `validate_neuron_minimum_dissolve_delay_to_vote_seconds`, mirroring the pattern already used for `initial_voting_period_seconds`:

```rust
pub const NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_FLOOR: u64 = ONE_DAY_SECONDS;

fn validate_neuron_minimum_dissolve_delay_to_vote_seconds(&self) -> Result<(), String> {
    ...
    if neuron_minimum_dissolve_delay_to_vote_seconds
        < Self::NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_FLOOR
    {
        return Err(format!(
            "neuron_minimum_dissolve

### Citations

**File:** rs/sns/governance/src/types.rs (L396-406)
```rust
    /// This is a lower bound for `initial_voting_period_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const INITIAL_VOTING_PERIOD_SECONDS_FLOOR: u64 = ONE_DAY_SECONDS;

    /// This is an upper bound for `wait_for_quiet_deadline_increase_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_CEILING: u64 = 30 * ONE_DAY_SECONDS;

    /// This is a lower bound for `wait_for_quiet_deadline_increase_seconds`. We're setting it to
    /// 1 instead of 0 because values of 0 are not currently well-tested.
    pub const WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_FLOOR: u64 = 1;
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

**File:** rs/sns/init/src/lib.rs (L1064-1085)
```rust
    fn validate_neuron_minimum_dissolve_delay_to_vote_seconds(&self) -> Result<(), String> {
        // As this is not currently configurable, pull the default value from
        let max_dissolve_delay_seconds = *NervousSystemParameters::with_default_values()
            .max_dissolve_delay_seconds
            .as_ref()
            .unwrap();

        let neuron_minimum_dissolve_delay_to_vote_seconds = self
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .ok_or_else(|| {
                "Error: neuron-minimum-dissolve-delay-to-vote-seconds must be specified".to_string()
            })?;

        if neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds {
            return Err(format!(
                "The minimum dissolve delay to vote ({neuron_minimum_dissolve_delay_to_vote_seconds}) cannot be greater than the max \
                dissolve delay ({max_dissolve_delay_seconds})"
            ));
        }

        Ok(())
    }
```

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
