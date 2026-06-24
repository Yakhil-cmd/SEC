### Title
Missing Floor Validation on `reject_cost_e8s` Allows Trivially-Cheap Proposal Spam in SNS Governance - (File: `rs/sns/governance/src/types.rs`)

### Summary
The `validate_reject_cost_e8s` function in SNS governance only checks that the field is present (not `None`), with no floor or proportionality check against `neuron_minimum_stake_e8s`. An SNS can be deployed — or its parameters updated via governance — with `reject_cost_e8s = 0` (or any arbitrarily small value), making proposal submission essentially free regardless of the minimum stake. This is the direct IC analog of the Parachain's `SlashAmount = 5` vs `MinStake = 1000 * PICA` misconfiguration.

### Finding Description
`validate_reject_cost_e8s` in `rs/sns/governance/src/types.rs` performs only a presence check:

```rust
fn validate_reject_cost_e8s(&self) -> Result<u64, String> {
    self.reject_cost_e8s
        .ok_or_else(|| "NervousSystemParameters.reject_cost_e8s must be set".to_string())
}
``` [1](#0-0) 

The same pattern appears in the SNS init path:

```rust
fn validate_proposal_reject_cost_e8s(&self) -> Result<(), String> {
    match self.proposal_reject_cost_e8s {
        Some(_) => Ok(()),
        None => Err("Error: proposal_reject_cost_e8s must be specified.".to_string()),
    }
}
``` [2](#0-1) 

In contrast, `validate_neuron_minimum_stake_e8s` enforces a meaningful floor (`neuron_minimum_stake_e8s > transaction_fee_e8s`), and parameters like `initial_voting_period_seconds` have explicit floor/ceiling bounds. No analogous cross-parameter check exists for `reject_cost_e8s` vs `neuron_minimum_stake_e8s`. [3](#0-2) 

The `make_proposal` enforcement gate is:

```rust
if proposer.stake_e8s() < reject_cost_e8s {
    return Err(...)
}
``` [4](#0-3) 

When `reject_cost_e8s = 0`, the condition `stake < 0` is always false for a `u64`, so any neuron passes unconditionally. When `reject_cost_e8s = 1` (1 e8s) against a `neuron_minimum_stake_e8s = 100_000_000` (1 token), the penalty is 0.000001 % of the minimum stake — structurally identical to the Parachain's `SlashAmount = 5` vs `MinStake = 1_000_000_000_000_000`.

An integration test already demonstrates that `reject_cost_e8s = 0` is accepted without error:

```rust
let system_params = NervousSystemParameters {
    transaction_fee_e8s: Some(100_000),
    reject_cost_e8s: Some(0),
    ..NervousSystemParameters::with_default_values()
};
``` [5](#0-4) 

The default values set `reject_cost_e8s = E8S_PER_TOKEN` (1 token) and `neuron_minimum_stake_e8s = E8S_PER_TOKEN` (1 token), which is proportional. But nothing in the validation chain enforces this ratio when an SNS deployer or a `ManageNervousSystemParameters` proposal supplies a different value. [6](#0-5) 

### Impact Explanation
With `reject_cost_e8s` set to 0 or a negligible value:

1. **Governance spam**: Any neuron holder with the minimum stake can submit an unlimited stream of proposals at zero economic cost. Each rejected proposal deducts nothing meaningful from the proposer's stake.
2. **Governance griefing**: The `max_number_of_proposals_with_ballots` ceiling can be saturated, blocking all legitimate proposals from being submitted until the spam proposals expire.
3. **Incentive collapse**: The economic disincentive that is supposed to deter frivolous or malicious proposals is entirely absent, undermining the integrity of the SNS governance process.

### Likelihood Explanation
The attacker is an SNS deployer (canister developer), which is an explicitly in-scope role. The misconfiguration can occur in two ways:

- **At initialization**: The deployer supplies `proposal_reject_cost_e8s = 0` (or 1) in the `SnsInitPayload`. The NNS `CreateServiceNervousSystem` proposal execution path calls `validate_pre_execution`, which calls `validate_proposal_reject_cost_e8s` — a check that passes for any `Some(_)` value.
- **Post-launch**: A `ManageNervousSystemParameters` proposal with `reject_cost_e8s = Some(1)` passes `NervousSystemParameters::validate()` because `validate_reject_cost_e8s` only checks presence. [7](#0-6) 

The likelihood is **medium**: a malicious or careless SNS deployer can trivially trigger this, and the validation gap is not guarded by any other layer.

### Recommendation
- **Short term**: Add a proportionality floor to `validate_reject_cost_e8s` in `rs/sns/governance/src/types.rs`, e.g., require `reject_cost_e8s >= transaction_fee_e8s` (matching the existing `neuron_minimum_stake_e8s` floor pattern), or require `reject_cost_e8s` to be at least some meaningful fraction of `neuron_minimum_stake_e8s`.
- **Short term**: Apply the same floor in `validate_proposal_reject_cost_e8s` in `rs/sns/init/src/lib.rs`.
- **Long term**: Audit all `NervousSystemParameters` fields for missing cross-parameter invariants (e.g., `reject_cost_e8s ≤ neuron_minimum_stake_e8s` to avoid locking out all proposers, and `reject_cost_e8s ≥ transaction_fee_e8s` to ensure it is non-trivial).

### Proof of Concept
1. Deploy an SNS via `CreateServiceNervousSystem` with `proposal_reject_cost_e8s = 1` and `neuron_minimum_stake_e8s = 100_000_000`. Both `validate_pre_execution` and `NervousSystemParameters::validate()` pass without error.
2. Stake a neuron with the minimum amount (1 token = 100,000,000 e8s).
3. Submit a `Motion` proposal. The `make_proposal` gate checks `100_000_000 < 1` → false → proposal accepted.
4. Vote to reject it. The neuron's `neuron_fees_e8s` increases by 1 e8s (0.000001 % of stake).
5. Repeat indefinitely. The proposer retains effectively 100 % of their stake after each rejection, facing no meaningful economic deterrent — exactly the `SlashAmount = 5` vs `MinStake = 1000 * PICA` scenario from the reference report.

### Citations

**File:** rs/sns/governance/src/types.rs (L469-494)
```rust
    pub fn with_default_values() -> Self {
        Self {
            reject_cost_e8s: Some(E8S_PER_TOKEN), // 1 governance token
            neuron_minimum_stake_e8s: Some(E8S_PER_TOKEN), // 1 governance token
            transaction_fee_e8s: Some(DEFAULT_TRANSFER_FEE.get_e8s()),
            max_proposals_to_keep_per_action: Some(100),
            initial_voting_period_seconds: Some(4 * ONE_DAY_SECONDS), // 4d
            wait_for_quiet_deadline_increase_seconds: Some(ONE_DAY_SECONDS), // 1d
            default_followees: Some(DefaultFollowees::default()),
            max_number_of_neurons: Some(200_000),
            neuron_minimum_dissolve_delay_to_vote_seconds: Some(6 * ONE_MONTH_SECONDS), // 6m
            max_followees_per_function: Some(15),
            max_dissolve_delay_seconds: Some(8 * ONE_YEAR_SECONDS), // 8y
            max_neuron_age_for_age_bonus: Some(4 * ONE_YEAR_SECONDS), // 4y
            max_number_of_proposals_with_ballots: Some(700),
            neuron_claimer_permissions: Some(Self::default_neuron_claimer_permissions()),
            neuron_grantable_permissions: Some(NeuronPermissionList::default()),
            max_number_of_principals_per_neuron: Some(5),
            voting_rewards_parameters: Some(VotingRewardsParameters::with_default_values()),
            max_dissolve_delay_bonus_percentage: Some(100),
            max_age_bonus_percentage: Some(25),
            maturity_modulation_disabled: Some(false),
            automatically_advance_target_version: Some(true),
            custom_proposal_criticality: None,
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L596-600)
```rust
    /// Validates that the nervous system parameter reject_cost_e8s is well-formed.
    fn validate_reject_cost_e8s(&self) -> Result<u64, String> {
        self.reject_cost_e8s
            .ok_or_else(|| "NervousSystemParameters.reject_cost_e8s must be set".to_string())
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

**File:** rs/sns/init/src/lib.rs (L848-890)
```rust
    pub fn validate_pre_execution(&self) -> Result<Self, String> {
        let validation_fns = [
            self.validate_token_symbol(),
            self.validate_token_name(),
            self.validate_token_logo(),
            self.validate_token_distribution(),
            self.validate_participation_constraints(),
            self.validate_neuron_minimum_stake_e8s(),
            self.validate_neuron_minimum_dissolve_delay_to_vote_seconds(),
            self.validate_neuron_basket_construction_params(),
            self.validate_proposal_reject_cost_e8s(),
            self.validate_transaction_fee_e8s(),
            self.validate_fallback_controller_principal_ids(),
            self.validate_url(),
            self.validate_logo(),
            self.validate_description(),
            self.validate_name(),
            self.validate_initial_reward_rate_basis_points(),
            self.validate_final_reward_rate_basis_points(),
            self.validate_reward_rate_transition_duration_seconds(),
            self.validate_max_dissolve_delay_seconds(),
            self.validate_max_neuron_age_seconds_for_age_bonus(),
            self.validate_max_dissolve_delay_bonus_percentage(),
            self.validate_max_age_bonus_percentage(),
            self.validate_initial_voting_period_seconds(),
            self.validate_wait_for_quiet_deadline_increase_seconds(),
            self.validate_dapp_canisters(),
            self.validate_confirmation_text(),
            self.validate_restricted_countries(),
            // Ensure that the values that can only be known after the execution
            // of the CreateServiceNervousSystem proposal are not set.
            self.validate_nns_proposal_id_pre_execution(),
            self.validate_swap_start_timestamp_seconds_pre_execution(),
            self.validate_swap_due_timestamp_seconds_pre_execution(),
            self.validate_neurons_fund_participation_constraints(true),
            self.validate_neurons_fund_participation(),
            // Obsolete fields are not set
            self.validate_min_icp_e8s(),
            self.validate_max_icp_e8s(),
        ];

        self.join_validation_results(&validation_fns)
    }
```

**File:** rs/sns/init/src/lib.rs (L1010-1015)
```rust
    fn validate_proposal_reject_cost_e8s(&self) -> Result<(), String> {
        match self.proposal_reject_cost_e8s {
            Some(_) => Ok(()),
            None => Err("Error: proposal_reject_cost_e8s must be specified.".to_string()),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L3519-3526)
```rust
        // If the current stake of the proposer neuron is less than the cost
        // of having a proposal rejected, the neuron cannot make a proposal.
        if proposer.stake_e8s() < reject_cost_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Neuron doesn't have enough stake to submit proposal.",
            ));
        }
```

**File:** rs/sns/integration_tests/src/nervous_system_parameters.rs (L23-27)
```rust
        let system_params = NervousSystemParameters {
            transaction_fee_e8s: Some(100_000),
            reject_cost_e8s: Some(0),
            ..NervousSystemParameters::with_default_values()
        };
```
