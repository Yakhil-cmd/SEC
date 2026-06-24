### Title
SNS `ManageNervousSystemParameters` Allows Setting `neuron_minimum_dissolve_delay_to_vote_seconds` to Zero, Bypassing Staking Lock-up Requirement — (File: `rs/sns/governance/src/types.rs`)

### Summary
The SNS governance canister's `NervousSystemParameters` validation does not enforce a minimum floor on `neuron_minimum_dissolve_delay_to_vote_seconds`. A `ManageNervousSystemParameters` proposal setting this field to `0` passes all validation checks and, once executed, allows dissolved neurons (with zero lock-up) to receive ballots and vote on all subsequent proposals, undermining the staking mechanism that is the foundation of SNS governance security.

### Finding Description
`validate_neuron_minimum_dissolve_delay_to_vote_seconds` in `rs/sns/governance/src/types.rs` enforces only one constraint: the value must not exceed `max_dissolve_delay_seconds`. There is no floor check. [1](#0-0) 

Setting `neuron_minimum_dissolve_delay_to_vote_seconds = Some(0)` satisfies `0 <= max_dissolve_delay_seconds` and passes `NervousSystemParameters::validate()` cleanly. [2](#0-1) 

`perform_manage_nervous_system_parameters` applies the proposed value via `inherit_from` and then calls `validate()`. Because `Some(0)` passes validation, the new parameter is committed to `self.proto.parameters`. [3](#0-2) 

After execution, `make_proposal` reads `neuron_minimum_dissolve_delay_to_vote_seconds` to gate ballot assignment. With the value at `0`, the check `proposer_dissolve_delay < min_dissolve_delay_for_vote` is `dissolve_delay < 0`, which is always false for a `u64`, so every neuron — including fully dissolved ones — receives a ballot and can vote. [4](#0-3) 

The default value is `6 * ONE_MONTH_SECONDS`, and the NNS analog (`VotingPowerEconomics`) correctly enforces a hard lower bound of 14 days via `NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS`. No equivalent floor exists for SNS. [5](#0-4) 

### Impact Explanation
Once `neuron_minimum_dissolve_delay_to_vote_seconds = 0` is in effect:

1. Any neuron, including fully dissolved ones with no lock-up, receives a ballot on every new proposal.
2. An attacker who accumulated SNS tokens can immediately dissolve their neurons, recover their tokens, and still retain full voting power on all open and future proposals.
3. This allows passing arbitrary subsequent `ManageNervousSystemParameters` proposals (e.g., setting `reject_cost_e8s = 0` to eliminate spam protection, or `max_dissolve_delay_bonus_percentage` to extreme values), `UpgradeSnsControlledCanister` proposals to install malicious code, or `TransferSnsTreasuryFunds` proposals to drain the treasury.
4. The staking mechanism — the primary economic disincentive against governance attacks — is completely neutralized.

### Likelihood Explanation
Medium. The attacker must hold or coordinate a majority of SNS voting power at the time the proposal is decided. This is analogous to the "Madman" or collusion scenario described in the reference report: a well-resourced actor acquires a majority stake, passes the parameter change, exploits the resulting open governance, and exits. Because SNS tokens are freely tradeable and SNS projects vary widely in token distribution, achieving a transient majority is realistic for smaller or newly launched SNS DAOs. The attack is economically self-funding: the attacker can dissolve neurons and recover capital immediately after the parameter change takes effect.

### Recommendation
Add a minimum floor to `validate_neuron_minimum_dissolve_delay_to_vote_seconds` in `rs/sns/governance/src/types.rs`, mirroring the NNS `NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS` lower bound of 14 days:

```rust
const NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_FLOOR: u64 = 14 * ONE_DAY_SECONDS;

if neuron_minimum_dissolve_delay_to_vote_seconds < NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_FLOOR {
    return Err(format!(
        "neuron_minimum_dissolve_delay_to_vote_seconds ({}) must be at least {} seconds (14 days)",
        neuron_minimum_dissolve_delay_to_vote_seconds,
        NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_FLOOR,
    ));
}
```

Additionally, audit all other `NervousSystemParameters` fields that lack floor checks (`reject_cost_e8s`, `neuron_minimum_stake_e8s`, `transaction_fee_e8s`) to ensure no analogous zero-value invariant violations exist.

### Proof of Concept

**Entry path**: SNS governance user (token holder) submitting an ingress message to the SNS governance canister's `manage_neuron` endpoint.

1. Attacker acquires majority SNS voting power (e.g., via open market purchase or coordination).
2. Attacker submits a `ManageNervousSystemParameters` proposal:
   ```
   NervousSystemParameters {
       neuron_minimum_dissolve_delay_to_vote_seconds: Some(0),
       ..Default::default()
   }
   ```
3. `validate_and_render_manage_nervous_system_parameters` calls `NervousSystemParameters::validate()`. The check `0 > max_dissolve_delay_seconds` is false; validation returns `Ok(())`. [6](#0-5) 
4. Proposal passes voting (attacker holds majority). `perform_manage_nervous_system_parameters` commits `neuron_minimum_dissolve_delay_to_vote_seconds = 0`.
5. Attacker calls `manage_neuron` with `Command::Disburse` on their neurons, recovering all staked tokens with no lock-up penalty.
6. Attacker's now-dissolved neurons still receive ballots on all future proposals because `dissolve_delay_seconds(now) = 0 >= 0`.
7. Attacker votes to pass a `TransferSnsTreasuryFunds` or `UpgradeSnsControlledCanister` proposal, draining the treasury or installing malicious canister code.
8. Attacker exits with both the recovered stake and the exploited treasury funds.

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

**File:** rs/nns/governance/src/network_economics.rs (L293-294)
```rust
    pub const NEURON_MINIMUM_DISSOLVE_DELAY_TO_VOTE_SECONDS_BOUNDS: RangeInclusive<u64> =
        (14 * ONE_DAY_SECONDS)..=(6 * ONE_MONTH_SECONDS);
```
