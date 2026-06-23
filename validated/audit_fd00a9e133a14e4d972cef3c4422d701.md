### Title
Unrestricted `ManageNervousSystemParameters` Upgrade Can Retroactively Break Existing SNS Neuron Staking and Voting Rights - (File: rs/sns/governance/src/governance.rs)

---

### Summary

The SNS governance canister allows any token-holder majority to pass a `ManageNervousSystemParameters` proposal that raises `neuron_minimum_stake_e8s` or `neuron_minimum_dissolve_delay_to_vote_seconds` to arbitrary new values. There is no check that existing neurons already satisfy the new parameters. This is the direct IC analog of the Gearbox `creditManager` parameter-upgrade bug: changing governance parameters retroactively invalidates the state of existing neurons, breaking their ability to submit proposals, vote, or refresh stake — without any warning or migration path.

---

### Finding Description

`ManageNervousSystemParameters` is a native SNS proposal action that, when executed, calls `perform_manage_nervous_system_parameters` in `rs/sns/governance/src/governance.rs`. This function merges the proposed parameters with the current ones and, if the merged result passes `NervousSystemParameters::validate()`, immediately overwrites `self.proto.parameters`. [1](#0-0) 

The validation in `NervousSystemParameters::validate()` only checks internal consistency of the *new* parameter set (e.g., `neuron_minimum_stake_e8s > transaction_fee_e8s`, `neuron_minimum_dissolve_delay_to_vote_seconds <= max_dissolve_delay_seconds`). It does **not** check whether existing neurons in the store satisfy the new parameters. [2](#0-1) 

Two concrete breakage paths exist:

**Path 1 — `neuron_minimum_stake_e8s` raised above existing neuron balances.**
After the parameter change, any neuron whose `cached_neuron_stake_e8s` is below the new minimum can no longer call `refresh_neuron` (it will be rejected with `InsufficientFunds`), and cannot submit proposals because the proposal submission check also enforces the minimum stake against `reject_cost_e8s`. [3](#0-2) 

**Path 2 — `neuron_minimum_dissolve_delay_to_vote_seconds` raised above existing neurons' dissolve delays.**
After the parameter change, `compute_ballots_for_new_proposal` silently excludes all neurons whose dissolve delay is below the new threshold. Neurons that previously had voting rights and were counted in the electoral roll are now excluded from all future proposals. [4](#0-3) 

The proto comment for `ManageNervousSystemParameters` itself acknowledges this asymmetry for `neuron_minimum_stake_e8s` (new minimum only applies to *future* neurons), but makes no such acknowledgment for `neuron_minimum_dissolve_delay_to_vote_seconds`, which is applied retroactively to all existing neurons at ballot-creation time. [5](#0-4) 

The `validate_and_render_manage_ledger_parameters` function for `ManageLedgerParameters` similarly performs no cross-check against existing neuron state when `transfer_fee` is raised. [6](#0-5) 

---

### Impact Explanation

- **Voting disenfranchisement**: Raising `neuron_minimum_dissolve_delay_to_vote_seconds` above the dissolve delay of a large fraction of existing neurons silently removes their voting power from all future proposals. If the disenfranchised neurons held a majority, the SNS governance quorum dynamics are permanently altered — future proposals may pass or fail with a different effective electorate than token-holders expected when they staked.
- **Neuron refresh lockout**: Raising `neuron_minimum_stake_e8s` above existing neuron balances prevents those neurons from refreshing their stake, effectively locking them out of governance participation and potentially trapping staked tokens.
- **Governance capture**: A token-holder majority (which may be a small coordinated group in a newly launched SNS) can use this to permanently disenfranchise a large portion of the community, analogous to the Gearbox `creditAccount` liquidation scenario.

---

### Likelihood Explanation

The `ManageNervousSystemParameters` proposal type is a standard, documented, non-critical SNS action. Any SNS neuron holder with sufficient voting power can submit and pass such a proposal. In newly launched SNS DAOs where token distribution is concentrated (e.g., developer neurons hold majority), this is trivially reachable. The code path is fully exercised in integration tests confirming the parameter change takes effect immediately. [7](#0-6) 

---

### Recommendation

1. Before applying new `NervousSystemParameters`, scan existing neurons and reject (or warn via proposal rendering) if the new `neuron_minimum_dissolve_delay_to_vote_seconds` would disenfranchise more than a configurable threshold of current voting power.
2. For `neuron_minimum_stake_e8s` increases, add a check that no existing neuron's cached stake falls below the new minimum, or explicitly document and enforce a grace period.
3. Consider classifying `ManageNervousSystemParameters` as a "critical" proposal (requiring a higher approval threshold) when the proposed changes would retroactively affect existing neurons' eligibility.

---

### Proof of Concept

1. Deploy an SNS with `neuron_minimum_dissolve_delay_to_vote_seconds = 6 months`. Many users stake neurons with exactly 6-month dissolve delays.
2. A coordinated majority submits a `ManageNervousSystemParameters` proposal setting `neuron_minimum_dissolve_delay_to_vote_seconds = 8 years`.
3. The proposal passes `validate()` because `8 years <= max_dissolve_delay_seconds (8 years)`.
4. `perform_manage_nervous_system_parameters` overwrites `self.proto.parameters` with no check on existing neurons.
5. On the next proposal, `compute_ballots_for_new_proposal` iterates all neurons and skips any with `dissolve_delay_seconds < 8 years` — which is the entire existing electorate except those who already had 8-year locks.
6. All previously eligible neurons are now permanently excluded from voting on all future proposals, with no recourse (they cannot reduce their dissolve delay once set). [8](#0-7) [9](#0-8)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L4258-4272)
```rust
        let min_stake = self
            .nervous_system_parameters_or_panic()
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");
        if balance.get_e8s() < min_stake {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Account does not have enough funds to refresh a neuron. \
                        Please make sure that account has at least {:?} e8s (was {:?} e8s)",
                    min_stake,
                    balance.get_e8s()
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L5248-5261)
```rust
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

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L675-685)
```rust
        /// Change the nervous system's parameters.
        /// Note that a change of a parameter will only affect future actions where
        /// this parameter is relevant.
        /// For example, NervousSystemParameters::neuron_minimum_stake_e8s specifies the
        /// minimum amount of stake a neuron must have, which is checked at the time when
        /// the neuron is created. If this NervousSystemParameter is decreased, all neurons
        /// created after this change will have at least the new minimum stake. However,
        /// neurons created before this change may have less stake.
        ///
        /// Id = 2.
        ManageNervousSystemParameters(super::NervousSystemParameters),
```

**File:** rs/sns/governance/src/proposal.rs (L1761-1799)
```rust
fn validate_and_render_manage_ledger_parameters(
    manage_ledger_parameters: &ManageLedgerParameters,
) -> Result<String, String> {
    let mut change = false;
    let mut render = "# Proposal to change ledger parameters:\n".to_string();
    let ManageLedgerParameters {
        transfer_fee,
        token_name,
        token_symbol,
        token_logo,
    } = manage_ledger_parameters;

    if let Some(transfer_fee) = transfer_fee {
        render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
        change = true;
    }
    if let Some(token_name) = token_name {
        ledger_validation::validate_token_name(token_name)?;
        render += &format!("# Set token name: {token_name}. \n",);
        change = true;
    }
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
        change = true;
    }
    if let Some(token_logo) = token_logo {
        ledger_validation::validate_token_logo(token_logo)?;
        render += &format!("# Set token logo: {token_logo}. \n",);
        change = true;
    }
    if !change {
        Err(String::from(
            "ManageLedgerParameters must change at least one value, all values are None",
        ))
    } else {
        Ok(render)
    }
}
```

**File:** rs/sns/integration_tests/src/proposals.rs (L763-789)
```rust
        // Update the NervousSystemParameters to contain a reject_cost_e8s greater than the
        // amount staked in the neuron
        let update_to_nervous_system_params = NervousSystemParameters {
            reject_cost_e8s: Some(200_000_000),
            ..Default::default()
        };

        sns_canisters
            .manage_nervous_system_parameters(
                &user.sender,
                &user.subaccount,
                update_to_nervous_system_params,
            )
            .await
            .expect("Expected updating NervousSystemParameters to succeed");

        // Submitting a proposal should fail due to the minimum stake not being greater
        // than reject_costs_e8s
        let expected_error = sns_canisters
            .make_proposal(&user.sender, &user.subaccount, proposal.clone())
            .await
            .expect_err("Expected make_proposal to error");

        assert_eq!(
            expected_error.error_type,
            ErrorType::PreconditionFailed as i32
        );
```
