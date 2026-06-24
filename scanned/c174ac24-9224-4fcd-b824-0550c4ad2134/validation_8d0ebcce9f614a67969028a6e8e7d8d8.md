### Title
`ManageNervousSystemParameters` Does Not Validate `neuron_minimum_dissolve_delay_to_vote_seconds` Against Existing Neurons, Enabling Permanent SNS Governance Lock - (File: rs/sns/governance/src/proposal.rs)

---

### Summary

`validate_and_render_manage_nervous_system_parameters` validates the proposed `NervousSystemParameters` only in isolation — it never checks whether the new `neuron_minimum_dissolve_delay_to_vote_seconds` value would leave at least one existing neuron eligible to vote. If a passed proposal raises this threshold above the dissolve delay of every existing neuron, `compute_ballots_for_new_proposal` returns `Err("No eligible voters.")` for every subsequent proposal, and `make_proposal` rejects every proposer whose dissolve delay is below the new minimum. The SNS governance canister becomes permanently ungovernable with no recovery path.

---

### Finding Description

`validate_and_render_manage_nervous_system_parameters` in `rs/sns/governance/src/proposal.rs` validates a `ManageNervousSystemParameters` proposal by calling `new_parameters.inherit_from(current_parameters).validate()`: [1](#0-0) 

`NervousSystemParameters::validate()` calls `validate_neuron_minimum_dissolve_delay_to_vote_seconds`, which only enforces that the new value does not exceed `max_dissolve_delay_seconds`: [2](#0-1) 

There is no check against the dissolve delays of neurons currently stored in `self.proto.neurons`. The full `validate()` chain is: [3](#0-2) 

After a proposal is executed via `perform_manage_nervous_system_parameters`, the new parameters are written unconditionally if `validate()` passes: [4](#0-3) 

Once the new `neuron_minimum_dissolve_delay_to_vote_seconds` is stored, every subsequent call to `make_proposal` checks the proposer's dissolve delay against the new minimum: [5](#0-4) 

And `compute_ballots_for_new_proposal` returns an error if the electoral roll is empty: [6](#0-5) 

If no neuron satisfies the new threshold, both paths fail permanently.

---

### Impact Explanation

An SNS governance canister becomes permanently ungovernable: no neuron can submit a proposal, no ballot can be created, and no governance action (including a corrective `ManageNervousSystemParameters` proposal) can ever be executed again. All SNS-controlled dapp canisters, treasury funds, and upgrade paths are frozen under the control of an unresponsive governance canister. There is no built-in recovery mechanism analogous to KintoWallet's 7-day `finishRecovery`.

---

### Likelihood Explanation

The scenario requires a legitimately-passed `ManageNervousSystemParameters` proposal — reachable by any SNS neuron holder with sufficient dissolve delay and stake. An SNS community that wants to raise the minimum staking requirement (a common governance action) can accidentally set `neuron_minimum_dissolve_delay_to_vote_seconds` to a value that most or all existing neurons do not yet satisfy. The code provides no warning or guard against this. The comment in `governance.proto` explicitly states the motivation for parameter floors/ceilings is to prevent "an un-upgradable (and thus stuck) governance canister," yet this specific cross-state inconsistency is not covered: [7](#0-6) 

---

### Recommendation

In `validate_and_render_manage_nervous_system_parameters` (or in `perform_manage_nervous_system_parameters`), after computing the merged parameters, verify that at least one neuron in `governance.proto.neurons` has a `dissolve_delay_seconds` ≥ the new `neuron_minimum_dissolve_delay_to_vote_seconds`. If no such neuron exists, reject the proposal with a descriptive error. This mirrors the existing check in `rs/sns/init/src/distributions.rs` that enforces at least one voting-eligible neuron at SNS initialization time: [8](#0-7) 

---

### Proof of Concept

```
// Initial state:
// - neuron_minimum_dissolve_delay_to_vote_seconds = 6 months (default)
// - Neuron A: dissolve_delay = 8 years  (proposer, majority stake)
// - Neuron B: dissolve_delay = 6 months (all other participants)

// Step 1: Neuron A submits ManageNervousSystemParameters proposal:
//   neuron_minimum_dissolve_delay_to_vote_seconds = 8 years
// Validation passes: 8 years <= max_dissolve_delay_seconds (8 years). OK.

// Step 2: Proposal passes (Neuron A has majority voting power).

// Step 3: perform_manage_nervous_system_parameters executes.
//   new_params.validate() passes. Parameters written.
//   neuron_minimum_dissolve_delay_to_vote_seconds is now 8 years.

// Step 4: Neuron A begins dissolving (dissolve_delay drops below 8 years).

// Step 5: Any attempt to call make_proposal now:
//   proposer.dissolve_delay_seconds(now) < 8 years  →  PreconditionFailed
//   compute_ballots_for_new_proposal: electoral_roll.is_empty()  →  Err("No eligible voters.")

// SNS governance is permanently locked. No corrective proposal can be submitted.
```

### Citations

**File:** rs/sns/governance/src/proposal.rs (L527-549)
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

**File:** rs/sns/governance/src/governance.rs (L3505-3517)
```rust
        let min_dissolve_delay_for_vote = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .expect("NervousSystemParameters must have min_dissolve_delay_for_vote");

        let proposer_dissolve_delay = proposer.dissolve_delay_seconds(now_seconds);
        if proposer_dissolve_delay < min_dissolve_delay_for_vote {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "The proposer's dissolve delay {proposer_dissolve_delay} is less than the minimum required dissolve delay of {min_dissolve_delay_for_vote}"
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L5255-5292)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }

        if total_power >= (u64::MAX as u128) {
            // The way the neurons are configured, the total voting
            // power on this proposal would overflow a u64!
            return Err("Voting power overflow.".to_string());
        }
        if electoral_roll.is_empty() {
            // Cannot make a proposal with no eligible voters.  This
            // is a precaution that shouldn't happen as we check that
            // the voter is allowed to vote.
            return Err("No eligible voters.".to_string());
        }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1111-1117)
```text
// For some of the values there are specified minimum values (floor) or maximum values
// (ceiling). The motivation for this is a) to prevent that the nervous system accidentally
// chooses parameters that result in an non-upgradable (and thus stuck) governance canister
// and b) to prevent the canister from growing too big (which could harm the other canisters
// on the subnet).
//
// Required invariant: the canister code assumes that all system parameters are always set.
```

**File:** rs/sns/init/src/distributions.rs (L230-243)
```rust
        let configured_at_least_one_voting_neuron = developer_distribution
            .developer_neurons
            .iter()
            .any(|neuron_distribution| {
                neuron_distribution.dissolve_delay_seconds
                    >= *neuron_minimum_dissolve_delay_to_vote_seconds
            });

        if !configured_at_least_one_voting_neuron {
            return Err(format!(
                "Error: There needs to be at least one voting-eligible neuron configured. To be \
                 eligible to vote, a neuron must have dissolve_delay_seconds of at least {neuron_minimum_dissolve_delay_to_vote_seconds}"
            ));
        }
```
