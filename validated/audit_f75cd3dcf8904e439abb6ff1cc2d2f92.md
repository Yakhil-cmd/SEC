### Title
Missing Upper-Bound Validation on `vesting_period_seconds` in SNS Developer Neuron Distribution Allows Permanently Locked Tokens - (File: rs/sns/init/src/distributions.rs)

### Summary

`FractionalDeveloperVotingPower::validate_neurons` in `rs/sns/init/src/distributions.rs` validates `dissolve_delay_seconds` against `max_dissolve_delay_seconds` but performs **no validation whatsoever on `vesting_period_seconds`**. An SNS creator can supply `vesting_period_seconds = u64::MAX` for developer neurons in a `CreateServiceNervousSystem` proposal. This passes all on-chain validation, is accepted by NNS Governance, and results in developer SNS tokens being permanently locked in neurons that can never begin dissolving — a direct analog to the "stuck funds" class described in the reference report.

---

### Finding Description

The `validate_neurons` function in `rs/sns/init/src/distributions.rs` is the sole validation gate for `NeuronDistribution` fields during SNS initialization. It checks:

- Missing controllers
- Duplicate `(controller, memo)` pairs
- Count exceeds `MAX_DEVELOPER_DISTRIBUTION_COUNT`
- Memo range conflicts with sale/basket ranges
- At least one voting-eligible neuron
- `dissolve_delay_seconds <= max_dissolve_delay_seconds` [1](#0-0) 

However, `vesting_period_seconds` — an `Option<u64>` field on `NeuronDistribution` — is read directly and stored into the genesis `Neuron` struct with no bounds check: [2](#0-1) 

The proto definition confirms `vesting_period_seconds` is an unconstrained `optional uint64`: [3](#0-2) 

The full validation chain invoked by NNS Governance at proposal submission time is:

`validate_create_service_nervous_system` → `SnsInitPayload::try_from` → `validate_pre_execution` → `validate_token_distribution` → `FractionalDeveloperVotingPower::validate` → `validate_neurons` [4](#0-3) 

None of these steps check `vesting_period_seconds` for any bound.

A neuron that is vesting is non-dissolving and **cannot start dissolving until the vesting duration has elapsed**. With `vesting_period_seconds = u64::MAX` (~585 billion years), the neuron can never exit the vesting state, and the developer's SNS tokens are permanently locked.

---

### Impact Explanation

**Impact: High** — Developer SNS tokens are permanently locked in neurons that can never dissolve. The `vesting_period_seconds` field prevents the neuron from entering the dissolving state until vesting completes: [5](#0-4) 

A developer who misconfigures (or is deceived into configuring) `vesting_period_seconds = u64::MAX` loses permanent access to their SNS token stake. Because developer neurons hold governance tokens, this also degrades the SNS's long-term governance capacity. The tokens are not recoverable — there is no administrative override path once the SNS is deployed.

---

### Likelihood Explanation

**Likelihood: Medium** — The `vesting_period_seconds` field is an optional duration that SNS creators set manually in YAML configuration files or via CLI flags. The field is documented as a "long-term commitment" mechanism, and there is no tooling warning or protocol-level guard against setting it to an extreme value. A developer intending to set a 3-year vesting period could accidentally supply a value in milliseconds instead of seconds (e.g., `94608000000` instead of `94608000`), or a malicious co-founder could supply `u64::MAX` to permanently lock partner neurons. The `sns-cli` friendly config path does not add any bounds check on `vesting_period` either: [6](#0-5) 

---

### Recommendation

Add an upper-bound check on `vesting_period_seconds` inside `validate_neurons` in `rs/sns/init/src/distributions.rs`. A sensible ceiling (e.g., `8 * ONE_YEAR_SECONDS`, matching the maximum dissolve delay bonus duration used elsewhere in the system) should be enforced:

```rust
if let Some(vesting_period) = neuron_distribution.vesting_period_seconds {
    if vesting_period > MAX_VESTING_PERIOD_SECONDS {
        return Err(format!(
            "Error: Developer neuron for {:?} has vesting_period_seconds ({}) \
             exceeding the maximum allowed ({})",
            neuron_distribution.controller,
            vesting_period,
            MAX_VESTING_PERIOD_SECONDS,
        ));
    }
}
```

A lower bound of `0` (i.e., no vesting) is already implicitly allowed since the field is `Option<u64>`. The upper bound constant should be defined alongside `MAX_DEVELOPER_DISTRIBUTION_COUNT`: [7](#0-6) 

---

### Proof of Concept

1. An SNS creator constructs a `CreateServiceNervousSystem` proposal with:
   ```yaml
   Neurons:
     - principal: <developer_principal>
       stake: 1000 tokens
       memo: 0
       dissolve_delay: 1 year
       vesting_period: 18446744073709551615  # u64::MAX seconds
   ```

2. The proposal is submitted to NNS Governance. `validate_create_service_nervous_system` calls `SnsInitPayload::try_from`, which calls `validate_pre_execution`, which calls `validate_token_distribution`, which calls `FractionalDeveloperVotingPower::validate`, which calls `validate_neurons`.

3. `validate_neurons` checks `dissolve_delay_seconds` against `max_dissolve_delay_seconds` but **never reads or checks `vesting_period_seconds`**: [8](#0-7) 

4. The proposal passes validation and is adopted. At SNS genesis, `create_neuron` stores `vesting_period_seconds = Some(u64::MAX)` directly into the genesis `Neuron`: [9](#0-8) 

5. The developer neuron is created in `PreInitializationSwap` mode with `vesting_period_seconds = u64::MAX`. It can never start dissolving. The developer's 1000 SNS tokens are permanently locked with no recovery path.

### Citations

**File:** rs/sns/init/src/distributions.rs (L30-30)
```rust
pub const MAX_DEVELOPER_DISTRIBUTION_COUNT: usize = 100;
```

**File:** rs/sns/init/src/distributions.rs (L130-162)
```rust
            vesting_period_seconds,
        ) = (
            neuron_distribution.controller()?,
            neuron_distribution.stake_e8s,
            neuron_distribution.memo,
            neuron_distribution.dissolve_delay_seconds,
            neuron_distribution.vesting_period_seconds,
        );

        let subaccount = compute_neuron_staking_subaccount(principal_id, subaccount_memo);

        let permission = NeuronPermission {
            principal: Some(principal_id),
            permission_type: parameters
                .neuron_claimer_permissions
                .as_ref()
                .expect("NervousSystemParameters.neuron_claimer_permissions must be present")
                .permissions
                .clone(),
        };

        Ok(Neuron {
            id: Some(NeuronId {
                id: subaccount.to_vec(),
            }),
            permissions: vec![permission],
            cached_neuron_stake_e8s: stake_e8s,
            followees: btreemap! {},
            dissolve_state: Some(DissolveState::DissolveDelaySeconds(dissolve_delay_seconds)),
            voting_power_percentage_multiplier,
            vesting_period_seconds,
            ..Default::default()
        })
```

**File:** rs/sns/init/src/distributions.rs (L165-262)
```rust
    /// Validate the NeuronDistributions in the developer bucket.
    fn validate_neurons(
        &self,
        developer_distribution: &DeveloperDistribution,
        nervous_system_parameters: &NervousSystemParameters,
    ) -> Result<(), String> {
        let neuron_minimum_dissolve_delay_to_vote_seconds = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .as_ref()
            .expect("Expected NervousSystemParameters.neuron_minimum_dissolve_delay_to_vote_seconds to be set");

        let max_dissolve_delay_seconds = nervous_system_parameters
            .max_dissolve_delay_seconds
            .as_ref()
            .expect("Expected NervousSystemParameters.max_dissolve_delay_seconds to be set");

        let missing_developer_principals_count = developer_distribution
            .developer_neurons
            .iter()
            .filter(|neuron_distribution| neuron_distribution.controller.is_none())
            .count();

        if missing_developer_principals_count != 0 {
            return Err(format!(
                "Error: {missing_developer_principals_count} developer_neurons are missing controllers"
            ));
        }

        let deduped_dev_neurons = developer_distribution
            .developer_neurons
            .iter()
            .map(|neuron_distribution| {
                (
                    (neuron_distribution.controller, neuron_distribution.memo),
                    neuron_distribution.stake_e8s,
                )
            })
            .collect::<BTreeMap<_, _>>();

        if deduped_dev_neurons.len() != developer_distribution.developer_neurons.len() {
            return Err(
                "Error: Neurons with the same controller and memo found in developer_neurons"
                    .to_string(),
            );
        }

        if deduped_dev_neurons.len() > MAX_DEVELOPER_DISTRIBUTION_COUNT {
            return Err(format!(
                "Error: The number of developer neurons must be less than {}. Current count is {}",
                MAX_DEVELOPER_DISTRIBUTION_COUNT,
                deduped_dev_neurons.len(),
            ));
        }

        for (controller, memo) in deduped_dev_neurons.keys() {
            if NEURON_BASKET_MEMO_RANGE_START <= *memo && *memo <= SALE_NEURON_MEMO_RANGE_END {
                return Err(format!(
                    "Error: Developer neuron with controller {} cannot have a memo in the range {} to {}",
                    controller.unwrap(),
                    NEURON_BASKET_MEMO_RANGE_START,
                    SALE_NEURON_MEMO_RANGE_END
                ));
            }
        }

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

        let misconfigured_dissolve_delay_principals: Vec<PrincipalId> = developer_distribution
            .developer_neurons
            .iter()
            .filter(|neuron_distribution| {
                neuron_distribution.dissolve_delay_seconds > *max_dissolve_delay_seconds
            })
            .map(|neuron_distribution| neuron_distribution.controller.unwrap())
            .collect();

        if !misconfigured_dissolve_delay_principals.is_empty() {
            return Err(format!(
                "Error: The following PrincipalIds have a dissolve_delay_seconds configured greater than \
                 the allowed max_dissolve_delay_seconds ({max_dissolve_delay_seconds}): {misconfigured_dissolve_delay_principals:?}"
            ));
        }

        Ok(())
    }
```

**File:** rs/sns/init/proto/ic_sns_init/pb/v1/sns_init.proto (L320-328)
```text
  // The duration that this neuron is vesting.
  //
  // A neuron that is vesting is non-dissolving and cannot start dissolving until the vesting duration has elapsed.
  // Vesting can be used to lock a neuron more than the max allowed dissolve delay. This allows devs and members of
  // a particular SNS instance to prove their long-term commitment to the community. For example, the max dissolve delay
  // for a particular SNS instance might be 1 year, but the devs of the project may set their vesting duration to 3
  // years and dissolve delay to 1 year in order to prove that they are making a minimum 4 year commitment to the
  // project.
  optional uint64 vesting_period_seconds = 5;
```

**File:** rs/nns/governance/src/governance.rs (L5037-5051)
```rust
    fn validate_create_service_nervous_system(
        &self,
        create_service_nervous_system: &CreateServiceNervousSystem,
    ) -> Result<(), GovernanceError> {
        // Must be able to convert to a valid SnsInitPayload.
        let conversion_result = SnsInitPayload::try_from(ApiCreateServiceNervousSystem::from(
            create_service_nervous_system.clone(),
        ));

        let validated = conversion_result.map_err(|e| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Invalid CreateServiceNervousSystem: {e}"),
            )
        })?;
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L226-234)
```text
  // The duration that this neuron is vesting.
  //
  // A neuron that is vesting is non-dissolving and cannot start dissolving until the vesting duration has elapsed.
  // Vesting can be used to lock a neuron more than the max allowed dissolve delay. This allows devs and members of
  // a particular SNS instance to prove their long-term commitment to the community. For example, the max dissolve delay
  // for a particular SNS instance might be 1 year, but the devs of the project may set their vesting duration to 3
  // years and dissolve delay to 1 year in order to prove that they are making a minimum 4 year commitment to the
  // project.
  optional uint64 vesting_period_seconds = 17;
```

**File:** rs/sns/cli/src/init_config_file/friendly.rs (L240-244)
```rust
    #[serde(with = "ic_nervous_system_humanize::serde::duration")]
    dissolve_delay: nervous_system_pb::Duration,

    #[serde(with = "ic_nervous_system_humanize::serde::duration")]
    vesting_period: nervous_system_pb::Duration,
```
