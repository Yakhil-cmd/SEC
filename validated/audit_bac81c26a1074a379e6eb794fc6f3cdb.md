### Title
SNS Governance Permanently Bricked by `ManageNervousSystemParameters` Raising `neuron_minimum_dissolve_delay_to_vote_seconds` Above All Existing Neuron Dissolve Delays - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister allows any token holder with a sufficiently-locked neuron to submit a `ManageNervousSystemParameters` proposal that raises `neuron_minimum_dissolve_delay_to_vote_seconds` to a value exceeding the dissolve delay of every existing neuron. The parameter validation only checks the new value against `max_dissolve_delay_seconds`; it never checks whether any existing neuron would remain eligible to vote. Once the proposal executes, `compute_ballots_for_new_proposal` permanently returns `Err("No eligible voters.")` for every subsequent proposal, including the remediation proposal that would lower the threshold back. The SNS governance is irreversibly bricked.

---

### Finding Description

`NervousSystemParameters.neuron_minimum_dissolve_delay_to_vote_seconds` is a live governance parameter that any SNS community can change via a `ManageNervousSystemParameters` proposal. The validation function `validate_neuron_minimum_dissolve_delay_to_vote_seconds` enforces only one constraint: the new value must not exceed `max_dissolve_delay_seconds` (default 8 years). [1](#0-0) 

It does **not** check whether any existing neuron has a dissolve delay at or above the proposed new threshold. If the new threshold exceeds every neuron's dissolve delay, the entire electoral roll becomes empty.

Every call to `make_proposal` invokes `compute_ballots_for_new_proposal`, which iterates over all neurons and skips any whose dissolve delay is below `min_dissolve_delay_for_vote`. If the resulting `electoral_roll` is empty, the function returns a hard error: [2](#0-1) 

This error propagates back through `make_proposal`: [3](#0-2) 

Additionally, `make_proposal` independently rejects any proposer whose own dissolve delay is below the threshold: [4](#0-3) 

There is no "emergency bypass" for `ManageNervousSystemParameters` proposals in SNS governance — unlike NNS governance, which has `allowed_when_resources_are_low()` for certain critical proposal types. The SNS `compute_ballots_for_new_proposal` check is unconditional. [5](#0-4) 

The `perform_manage_nervous_system_parameters` execution path applies the new parameters without any cross-check against the live neuron population: [6](#0-5) 

The `NervousSystemParameters` documentation itself acknowledges the risk of accidentally choosing parameters that result in a "stuck" governance canister, but the guard implemented only covers structural validity, not consistency with the live neuron state: [7](#0-6) 

---

### Impact Explanation

Once `neuron_minimum_dissolve_delay_to_vote_seconds` is set above the dissolve delay of every existing neuron:

1. No neuron can submit a proposal (proposer dissolve delay check fails).
2. Even if a proposal could be submitted, `compute_ballots_for_new_proposal` returns `"No eligible voters."`.
3. The remediation proposal (lowering the threshold) cannot be submitted for the same reason.
4. The SNS governance canister is permanently bricked: no proposals can be created, voted on, or executed.
5. Downstream consequences include: the SNS dapp cannot be upgraded, treasury funds cannot be moved, and the SNS is effectively dead.

This is a direct analog to M-13: a governance parameter set to an inconsistent value relative to the current live state causes a core function to permanently fail, rendering the system unusable.

---

### Likelihood Explanation

The scenario can arise from accidental misconfiguration — a well-intentioned governance participant proposes "require longer lock-up for governance participation" without realizing that no existing neuron meets the new bar. The `validate_neuron_minimum_dissolve_delay_to_vote_seconds` function gives no warning about this. The M-13 report itself was classified as non-high-severity precisely because the parties involved (HSG deployer / SNS governance voters) are incentive-aligned with fixing the mistake — but fixing it is impossible once the governance is bricked. The SNS governance `make_proposal` endpoint is reachable by any SNS token holder with a staked neuron via standard ingress.

---

### Recommendation

`validate_and_render_manage_nervous_system_parameters` (and/or `perform_manage_nervous_system_parameters`) should verify that at least one existing neuron has a dissolve delay ≥ the proposed `neuron_minimum_dissolve_delay_to_vote_seconds` before accepting the proposal. Alternatively, add a floor check analogous to the NNS `NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS` that prevents the value from being raised above the maximum dissolve delay of any currently-staked neuron. [8](#0-7) 

---

### Proof of Concept

1. An SNS is deployed with all neurons having `dissolve_delay_seconds = ONE_YEAR_SECONDS` (the default `neuron_minimum_dissolve_delay_to_vote_seconds` is 6 months, so all neurons are eligible).
2. A token holder submits a `ManageNervousSystemParameters` proposal setting `neuron_minimum_dissolve_delay_to_vote_seconds = 7 * ONE_YEAR_SECONDS` (valid per `validate_neuron_minimum_dissolve_delay_to_vote_seconds` since `7y < max_dissolve_delay_seconds = 8y`).
3. The proposal passes and `perform_manage_nervous_system_parameters` updates the live parameter.
4. Any subsequent call to `make_proposal` by any neuron (all have 1-year dissolve delays) hits the check at line 3510 and returns `PreconditionFailed`.
5. Even if that check were bypassed, `compute_ballots_for_new_proposal` at line 5287 returns `Err("No eligible voters.")`.
6. The SNS governance is permanently bricked. No proposal — including one to lower `neuron_minimum_dissolve_delay_to_vote_seconds` — can ever be submitted again. [1](#0-0) [9](#0-8)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L3509-3517)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L3557-3560)
```rust
        let (_, electoral_roll) = self
            .compute_ballots_for_new_proposal()
            .map_err(|err| GovernanceError::new_with_message(ErrorType::PreconditionFailed, err))?;

```

**File:** rs/sns/governance/src/governance.rs (L5225-5295)
```rust
    /// Computes the total potential voting power of the governance canister and ballots.
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
        let now_seconds = self.env.now();

        let nervous_system_parameters = self.nervous_system_parameters_or_panic();

        // Voting power bonus parameters.
        let max_dissolve_delay = nervous_system_parameters
            .max_dissolve_delay_seconds
            .expect("NervousSystemParameters must have max_dissolve_delay_seconds");

        let max_age_bonus = nervous_system_parameters
            .max_neuron_age_for_age_bonus
            .expect("NervousSystemParameters must have max_neuron_age_for_age_bonus");

        let max_dissolve_delay_bonus_percentage = nervous_system_parameters
            .max_dissolve_delay_bonus_percentage
            .expect("NervousSystemParameters must have max_dissolve_delay_bonus_percentage");

        let max_age_bonus_percentage = nervous_system_parameters
            .max_age_bonus_percentage
            .expect("NervousSystemParameters must have max_age_bonus_percentage");

        let min_dissolve_delay_for_vote = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .expect("NervousSystemParameters must have min_dissolve_delay_for_vote");

        let mut electoral_roll = BTreeMap::<String, Ballot>::new();
        let mut total_power: u128 = 0;

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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1630-1638)
```rust
/// The nervous system's parameters, which are parameters that can be changed, via proposals,
/// by each nervous system community.
/// For some of the values there are specified minimum values (floor) or maximum values
/// (ceiling). The motivation for this is a) to prevent that the nervous system accidentally
/// chooses parameters that result in an non-upgradable (and thus stuck) governance canister
/// and b) to prevent the canister from growing too big (which could harm the other canisters
/// on the subnet).
///
/// Required invariant: the canister code assumes that all system parameters are always set.
```

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
