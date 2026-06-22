### Title
`ManageNervousSystemParameters` Reduces `max_number_of_neurons` Below Current Neuron Count, Permanently Blocking New Neuron Creation - (`rs/sns/governance/src/governance.rs`)

### Summary

The SNS Governance canister's `perform_manage_nervous_system_parameters` function applies a new `max_number_of_neurons` value without checking whether the current live neuron count already exceeds the proposed new limit. Because SNS governance proposals have a multi-day voting period, innocent users can create neurons between proposal submission and execution, causing the executed proposal to set `max_number_of_neurons` below the actual neuron count. This permanently blocks all new neuron creation until a second governance proposal corrects the limit.

### Finding Description

When a `ManageNervousSystemParameters` proposal is submitted to reduce `max_number_of_neurons`, the proposal validation in `validate_and_render_manage_nervous_system_parameters` calls `new_parameters.inherit_from(current_parameters).validate()`. [1](#0-0) 

The `validate()` call dispatches to `validate_max_number_of_neurons`, which only checks that the value is within the absolute range `(0, MAX_NUMBER_OF_NEURONS_CEILING]`: [2](#0-1) 

It does **not** check whether the proposed value is `>= self.proto.neurons.len()` (the current live neuron count). The execution path in `perform_manage_nervous_system_parameters` also performs no such check — it simply calls `new_params.validate()` and, on success, writes the new parameters: [3](#0-2) 

The code comment at line 2601–2608 acknowledges that proposals can become stale relative to current state, but only considers the case of conflicting *parameter* proposals — not the case of the live neuron population growing during the voting period.

After execution, every call to `check_neuron_population_can_grow` will fail because `current_count + 1 > new_max`: [4](#0-3) 

This blocks `claim_neuron` and `claim_swap_neurons` for all future callers.

### Impact Explanation

All new neuron creation in the affected SNS is permanently denied until a second governance proposal raises `max_number_of_neurons` again. This is a denial-of-service against the SNS governance participation mechanism: no new principals can stake and claim neurons, no new swap participants can receive neurons, and the SNS community's ability to grow is frozen. The condition is self-correcting only via another governance vote, which itself requires a multi-day voting period.

### Likelihood Explanation

SNS governance proposals have voting periods of at least one day (floor `INITIAL_VOTING_PERIOD_SECONDS_FLOOR = ONE_DAY_SECONDS`). [5](#0-4) 

During this window, any SNS token holder can stake tokens and call `claim_neuron` — an unprivileged ingress action. In an active SNS with ongoing token distribution or swap activity, neuron creation during a voting period is routine and expected. The proposer has no way to atomically check the neuron count and set the limit; the check-then-set is inherently non-atomic across the voting period. The scenario requires no malicious actor: innocent users staking tokens during a legitimate parameter-reduction vote are sufficient to trigger the invariant violation.

### Recommendation

In `perform_manage_nervous_system_parameters`, before applying a new `max_number_of_neurons`, add a runtime check against the current neuron population:

```rust
if let Some(new_max) = new_params.max_number_of_neurons {
    let current_count = self.proto.neurons.len() as u64;
    if new_max < current_count {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Cannot set max_number_of_neurons ({new_max}) below the current \
                 neuron count ({current_count})"
            ),
        ));
    }
}
```

This mirrors the pattern already used for `reserved_cycles_limit` in the execution environment, where the current balance is checked before applying a new limit: [6](#0-5) 

### Proof of Concept

1. An SNS has 50 neurons. A governance proposal is submitted to set `max_number_of_neurons = 45`. At submission time, `validate_max_number_of_neurons` passes (45 > 0, 45 ≤ 200,000).

2. During the voting period (≥ 1 day), 10 innocent users stake SNS tokens and call `claim_neuron`, bringing the total to 60 neurons.

3. The proposal reaches quorum and is executed. `perform_manage_nervous_system_parameters` calls `new_params.validate()` — this passes because 45 is still a valid range value. `self.proto.parameters` is set with `max_number_of_neurons = 45`.

4. Any subsequent call to `claim_neuron` or `claim_swap_neurons` invokes `check_neuron_population_can_grow`, which evaluates `(60 + 1) > 45 = true` and returns `PreconditionFailed: "Cannot add neuron. Max number of neurons reached."` — permanently, until a new proposal raises the limit. [4](#0-3)

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

**File:** rs/sns/governance/src/types.rs (L396-398)
```rust
    /// This is a lower bound for `initial_voting_period_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const INITIAL_VOTING_PERIOD_SECONDS_FLOOR: u64 = ONE_DAY_SECONDS;
```

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

**File:** rs/sns/governance/src/governance.rs (L6363-6379)
```rust
    /// Checks whether new neurons can be added or whether the maximum number of neurons,
    /// as defined in the nervous system parameters, has already been reached.
    fn check_neuron_population_can_grow(&self) -> Result<(), GovernanceError> {
        let max_number_of_neurons = self
            .nervous_system_parameters_or_panic()
            .max_number_of_neurons
            .expect("NervousSystemParameters must have max_number_of_neurons");

        if (self.proto.neurons.len() as u64) + 1 > max_number_of_neurons {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Cannot add neuron. Max number of neurons reached.",
            ));
        }

        Ok(())
    }
```

**File:** rs/execution_environment/src/canister_manager.rs (L394-402)
```rust
        if let Some(limit) = settings.reserved_cycles_limit() {
            let canister_reserved_balance = canister.system_state.reserved_balance();
            if canister_reserved_balance > limit {
                return Err(CanisterManagerError::ReservedCyclesLimitIsTooLow {
                    cycles: canister_reserved_balance,
                    limit,
                });
            }
            canister.system_state.set_reserved_balance_limit(limit);
```
