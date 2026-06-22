### Title
Wrong Variable Used in SNS Init Validation Allows Invalid Governance Parameters - (File: rs/sns/init/src/lib.rs)

### Summary
`SnsInitPayload::validate_neuron_minimum_dissolve_delay_to_vote_seconds` validates `neuron_minimum_dissolve_delay_to_vote_seconds` against the **hardcoded default** `max_dissolve_delay_seconds` (8 years) instead of `self.max_dissolve_delay_seconds` (the actual value in the payload). This is a direct analog to the reported bug: checking the wrong variable during initialization validation.

### Finding Description
In `rs/sns/init/src/lib.rs`, the function `validate_neuron_minimum_dissolve_delay_to_vote_seconds` reads:

```rust
fn validate_neuron_minimum_dissolve_delay_to_vote_seconds(&self) -> Result<(), String> {
    // As this is not currently configurable, pull the default value from
    let max_dissolve_delay_seconds = *NervousSystemParameters::with_default_values()
        .max_dissolve_delay_seconds
        .as_ref()
        .unwrap();
    ...
    if neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds {
        return Err(...);
    }
    Ok(())
}
```

The comment claims `max_dissolve_delay_seconds` is "not currently configurable," but this is incorrect. `SnsInitPayload` has its own `max_dissolve_delay_seconds` field, which is populated by callers and propagated into `NervousSystemParameters` via `get_nervous_system_parameters()`:

```rust
NervousSystemParameters {
    ...
    max_dissolve_delay_seconds,   // taken from self
    ...
}
```

The correct implementation — used in `NervousSystemParameters::validate_neuron_minimum_dissolve_delay_to_vote_seconds` in `rs/sns/governance/src/types.rs` — reads `self.validate_max_dissolve_delay_seconds()` (i.e., the actual field value), not a hardcoded default.

### Impact Explanation
An SNS creator can submit an `SnsInitPayload` with:
- `max_dissolve_delay_seconds` = e.g. 1 year (smaller than the 8-year default)
- `neuron_minimum_dissolve_delay_to_vote_seconds` = e.g. 2 years (greater than `max_dissolve_delay_seconds`, but less than the 8-year default)

The `SnsInitPayload` validation passes (because `2 years < 8 years default`), but the resulting `NervousSystemParameters` has `neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds`. This is an invalid state: no neuron can ever reach the minimum dissolve delay required to vote, since it exceeds the maximum possible dissolve delay. This permanently breaks SNS governance — no proposals can ever be voted on.

If the SNS governance canister re-validates parameters on init and traps, the impact is a failed SNS deployment after passing pre-deployment validation, causing loss of deployment cycles and a confusing user experience. If it does not re-validate strictly, the SNS is deployed with permanently non-functional governance.

### Likelihood Explanation
Any SNS creator (unprivileged principal) can trigger this by submitting a custom `SnsInitPayload` with a non-default `max_dissolve_delay_seconds`. The SNS-W canister's `deploy_new_sns` endpoint is publicly callable. No privileged access is required. The misconfiguration is subtle and the misleading comment ("not currently configurable") makes it likely to persist.

### Recommendation
Replace the hardcoded default lookup with `self.max_dissolve_delay_seconds`:

```rust
fn validate_neuron_minimum_dissolve_delay_to_vote_seconds(&self) -> Result<(), String> {
    let max_dissolve_delay_seconds = self
        .max_dissolve_delay_seconds
        .ok_or_else(|| "Error: max_dissolve_delay_seconds must be specified".to_string())?;

    let neuron_minimum_dissolve_delay_to_vote_seconds = self
        .neuron_minimum_dissolve_delay_to_vote_seconds
        .ok_or_else(|| {
            "Error: neuron-minimum-dissolve-delay-to-vote-seconds must be specified".to_string()
        })?;

    if neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds {
        return Err(format!(...));
    }
    Ok(())
}
```

This mirrors the correct implementation in `NervousSystemParameters::validate_neuron_minimum_dissolve_delay_to_vote_seconds`.

### Proof of Concept

**Root cause** — `validate_neuron_minimum_dissolve_delay_to_vote_seconds` uses the hardcoded default instead of `self`: [1](#0-0) 

**`max_dissolve_delay_seconds` IS a configurable field in `SnsInitPayload`** — it is propagated into `NervousSystemParameters` via `get_nervous_system_parameters()`: [2](#0-1) 

**Correct implementation** — `NervousSystemParameters::validate_neuron_minimum_dissolve_delay_to_vote_seconds` correctly reads `self.validate_max_dissolve_delay_seconds()`: [3](#0-2) 

**Default value** — `NervousSystemParameters::with_default_values()` sets `max_dissolve_delay_seconds` to 8 years, which is what the buggy validation always checks against regardless of the payload: [4](#0-3) 

**Attack path**: A caller invokes `deploy_new_sns` on the SNS-W canister with an `SnsInitPayload` where `max_dissolve_delay_seconds = X` (X < 8 years) and `neuron_minimum_dissolve_delay_to_vote_seconds = Y` where `X < Y < 8 years`. `validate_post_execution()` calls `validate_neuron_minimum_dissolve_delay_to_vote_seconds()`, which checks `Y > 8 years` (false) and passes. The resulting SNS governance is initialized with `neuron_minimum_dissolve_delay_to_vote_seconds > max_dissolve_delay_seconds`, making governance permanently non-functional.

### Citations

**File:** rs/sns/init/src/lib.rs (L779-820)
```rust
            max_dissolve_delay_seconds,
            max_neuron_age_seconds_for_age_bonus: max_neuron_age_for_age_bonus,
            max_dissolve_delay_bonus_percentage,
            max_age_bonus_percentage,
            initial_voting_period_seconds,
            wait_for_quiet_deadline_increase_seconds,
            dapp_canisters: _,
            confirmation_text: _,
            restricted_countries: _,
            min_participants: _,
            min_icp_e8s: _,
            max_icp_e8s: _,
            min_direct_participation_icp_e8s: _,
            max_direct_participation_icp_e8s: _,
            min_participant_icp_e8s: _,
            max_participant_icp_e8s: _,
            swap_start_timestamp_seconds: _,
            swap_due_timestamp_seconds: _,
            neuron_basket_construction_parameters: _,
            nns_proposal_id: _,
            token_logo: _,
            neurons_fund_participation_constraints: _,
            neurons_fund_participation: _,
            custom_proposal_criticality,
        } = self.clone();

        let voting_rewards_parameters = Some(VotingRewardsParameters {
            reward_rate_transition_duration_seconds,
            initial_reward_rate_basis_points,
            final_reward_rate_basis_points,
            ..nervous_system_parameters.voting_rewards_parameters.unwrap()
        });

        NervousSystemParameters {
            neuron_claimer_permissions: Some(all_permissions.clone()),
            neuron_grantable_permissions: Some(all_permissions),
            transaction_fee_e8s,
            reject_cost_e8s,
            neuron_minimum_stake_e8s,
            neuron_minimum_dissolve_delay_to_vote_seconds,
            voting_rewards_parameters,
            max_dissolve_delay_seconds,
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

**File:** rs/sns/governance/src/types.rs (L481-481)
```rust
            max_dissolve_delay_seconds: Some(8 * ONE_YEAR_SECONDS), // 8y
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
