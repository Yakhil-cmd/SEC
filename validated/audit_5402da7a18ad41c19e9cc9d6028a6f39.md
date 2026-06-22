### Title
SNS `NervousSystemParameters.max_dissolve_delay_seconds` Lacks Upper-Bound Validation, Allowing Governance to Permanently Brick Proposal Submission — (`rs/sns/governance/src/types.rs`)

---

### Summary

`NervousSystemParameters.max_dissolve_delay_seconds` and `max_neuron_age_for_age_bonus` in SNS governance have no floor or ceiling bounds in their validation functions. Because `neuron_minimum_dissolve_delay_to_vote_seconds` is only constrained to be `<= max_dissolve_delay_seconds`, a governance actor can set both to an astronomically large value (e.g., `u64::MAX`). Once applied, no neuron can ever satisfy the dissolve-delay eligibility threshold, causing every subsequent call to `compute_ballots_for_new_proposal` to return `Err("No eligible voters.")` — permanently bricking the SNS governance.

---

### Finding Description

`validate_max_dissolve_delay_seconds` in `rs/sns/governance/src/types.rs` only asserts the field is `Some`; it enforces no floor and no ceiling:

```rust
fn validate_max_dissolve_delay_seconds(&self) -> Result<u64, String> {
    self.max_dissolve_delay_seconds.ok_or_else(|| {
        "NervousSystemParameters.max_dissolve_delay_seconds must be set".to_string()
    })
}
``` [1](#0-0) 

`validate_max_neuron_age_for_age_bonus` has the same deficiency — it only checks `is_some()`: [2](#0-1) 

`validate_neuron_minimum_dissolve_delay_to_vote_seconds` only rejects values **strictly greater than** `max_dissolve_delay_seconds`, so setting both to the same extreme value passes validation: [3](#0-2) 

Contrast this with parameters that do have proper bounds, e.g. `max_number_of_neurons` (floor = 1, ceiling = `MAX_NUMBER_OF_NEURONS_CEILING`) and `max_proposals_to_keep_per_action` (floor = 1, ceiling = `MAX_PROPOSALS_TO_KEEP_PER_ACTION_CEILING`): [4](#0-3) 

A `ManageNervousSystemParameters` proposal is validated at submission time via `validate_and_render_manage_nervous_system_parameters`, which calls `new_params.inherit_from(current).validate()`: [5](#0-4) 

And executed via `perform_manage_nervous_system_parameters`, which re-validates and then writes the new parameters: [6](#0-5) 

After the parameters are written, every new proposal triggers `compute_ballots_for_new_proposal`. The loop at line 5258 skips every neuron whose `dissolve_delay_seconds < min_dissolve_delay_for_vote`. With `min_dissolve_delay_for_vote = u64::MAX`, every neuron is skipped, `electoral_roll` is empty, and the function returns `Err("No eligible voters.")`: [7](#0-6) 

The `NervousSystemParameters` struct documentation itself acknowledges the intent to prevent stuck governance, but the missing bounds on these two fields undermine that goal: [8](#0-7) 

---

### Impact Explanation

Once a two-step governance attack succeeds (first raise `max_dissolve_delay_seconds` to `u64::MAX`, then raise `neuron_minimum_dissolve_delay_to_vote_seconds` to `u64::MAX`), **no further proposals can ever be submitted or voted on**. The SNS governance canister is permanently bricked: no upgrades, no parameter changes, no treasury actions. The only recovery path would be an NNS-level intervention to reinstall the canister, which is not guaranteed.

A secondary impact from `max_dissolve_delay_seconds = 0`: the dissolve-delay bonus is silently zeroed out for all neurons (the `if max_dissolve_delay_seconds > 0` guard in `voting_power` returns 0), distorting voting power without any error signal: [9](#0-8) 

---

### Likelihood Explanation

The attacker-controlled entry path is a standard `ManageNervousSystemParameters` ingress proposal, submittable by any neuron holder with sufficient stake. An SNS with a concentrated token distribution (common at launch) or one that has been gradually captured by a coordinated group can pass both proposals. The SNS documentation explicitly warns that parameters are chosen by governance, but provides no on-chain guardrails for these two fields. The risk is analogous to the Morpho `maxGas` finding: governance has no technical incentive barrier preventing the harmful parameter combination.

---

### Recommendation

Add explicit floor and ceiling constants for `max_dissolve_delay_seconds` and `max_neuron_age_for_age_bonus` in `validate_max_dissolve_delay_seconds` and `validate_max_neuron_age_for_age_bonus`, mirroring the pattern already used for `max_number_of_neurons` and `max_proposals_to_keep_per_action`. For example:

- `max_dissolve_delay_seconds`: floor = some minimum (e.g., 1 day), ceiling = e.g., 8 years in seconds.
- `max_neuron_age_for_age_bonus`: floor = 0 is acceptable (disables age bonus), ceiling = e.g., 8 years.
- Additionally, enforce that `neuron_minimum_dissolve_delay_to_vote_seconds` is strictly **less than** `max_dissolve_delay_seconds` (not merely `<=`), so at least neurons at the maximum dissolve delay are always eligible.

---

### Proof of Concept

1. An SNS neuron holder with majority voting power submits:
   ```
   ManageNervousSystemParameters {
       max_dissolve_delay_seconds: Some(u64::MAX),
       ..Default::default()
   }
   ```
   This passes `validate_max_dissolve_delay_seconds` (only checks `is_some()`). Proposal executes.

2. The same actor submits:
   ```
   ManageNervousSystemParameters {
       neuron_minimum_dissolve_delay_to_vote_seconds: Some(u64::MAX),
       ..Default::default()
   }
   ```
   This passes `validate_neuron_minimum_dissolve_delay_to_vote_seconds` because `u64::MAX <= u64::MAX`. Proposal executes.

3. Any subsequent call to `make_proposal` triggers `compute_ballots_for_new_proposal`. For every neuron `v`, `v.dissolve_delay_seconds(now) < u64::MAX` is true (no neuron has ~584 billion years of dissolve delay). All neurons are skipped. `electoral_roll.is_empty()` → `Err("No eligible voters.")`. The SNS is permanently bricked.

### Citations

**File:** rs/sns/governance/src/types.rs (L734-750)
```rust
    /// Validates that the nervous system parameter max_number_of_neurons is well-formed.
    fn validate_max_number_of_neurons(&self) -> Result<(), String> {
        let max_number_of_neurons = self.max_number_of_neurons.ok_or_else(|| {
            "NervousSystemParameters.max_number_of_neurons must be set".to_string()
        })?;

        if max_number_of_neurons > Self::MAX_NUMBER_OF_NEURONS_CEILING {
            Err(format!(
                "NervousSystemParameters.max_number_of_neurons must be less than {}",
                Self::MAX_NUMBER_OF_NEURONS_CEILING
            ))
        } else if max_number_of_neurons == 0 {
            Err("NervousSystemParameters.max_number_of_neurons must be greater than 0".to_string())
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L754-772)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L5255-5295)
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

        Ok((total_power as u64, electoral_roll))
    }
```

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L1146-1154)
```rust
/// The nervous system's parameters, which are parameters that can be changed, via proposals,
/// by each nervous system community.
/// For some of the values there are specified minimum values (floor) or maximum values
/// (ceiling). The motivation for this is a) to prevent that the nervous system accidentally
/// chooses parameters that result in an un-upgradable (and thus stuck) governance canister
/// and b) to prevent the canister from growing too big (which could harm the other canisters
/// on the subnet).
///
/// Required invariant: the canister code assumes that all system parameters are always set.
```

**File:** rs/sns/governance/src/neuron.rs (L213-219)
```rust
        let d_stake = stake
            + if max_dissolve_delay_seconds > 0 {
                (stake * d * max_dissolve_delay_bonus_percentage as u128)
                    / (100 * max_dissolve_delay_seconds as u128)
            } else {
                0
            };
```
