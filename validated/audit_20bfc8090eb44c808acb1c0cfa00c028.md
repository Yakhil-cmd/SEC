### Title
Missing Execution-Time Capacity Check Allows Exceeding `MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS` - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister validates that the number of registered `GenericNervousSystemFunction`s does not exceed `MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS` at proposal-submission time, but this check is absent from the execution function `perform_add_generic_nervous_system_function`. Two concurrent `AddGenericNervousSystemFunction` proposals, each with a distinct function ID, can both pass submission-time validation when the registry is one slot below the cap, then both execute successfully, pushing the registry above the intended maximum.

### Finding Description

**Proposal-submission validation** (`validate_and_render_add_generic_nervous_system_function` in `rs/sns/governance/src/proposal.rs`):

```rust
if existing_functions.len() >= MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS {
    return Err("Reached maximum number of allowed GenericNervousSystemFunctions".to_string());
}
``` [1](#0-0) 

This check is performed against the state at proposal-submission time.

**Execution function** (`perform_add_generic_nervous_system_function` in `rs/sns/governance/src/governance.rs`):

```rust
fn perform_add_generic_nervous_system_function(
    &mut self,
    nervous_system_function: NervousSystemFunction,
) -> Result<(), GovernanceError> {
    let id = nervous_system_function.id;

    if nervous_system_function.is_native() { return Err(...); }

    if is_registered_function_id(id, &self.proto.id_to_nervous_system_functions) {
        return Err(...);  // duplicate-ID check only
    }
    // ... canister-target checks ...
    self.proto.id_to_nervous_system_functions.insert(id, nervous_system_function);
    Ok(())
}
``` [2](#0-1) 

There is **no re-check** of `id_to_nervous_system_functions.len() >= MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS` at execution time. The duplicate-ID guard (`is_registered_function_id`) only prevents the same ID from being added twice; it does not prevent two proposals with *different* IDs from both succeeding when the registry is at `MAX - 1`.

By contrast, `perform_manage_nervous_system_parameters` explicitly re-validates the merged parameters at execution time and even carries a code comment acknowledging the concurrent-proposal risk:

> "Even though proposals are validated when they are first made, this is still possible, because the inner value of a ManageNervousSystemParameters proposal is only valid with respect to the current nervous_system_parameters() at the time when the proposal was first made." [3](#0-2) 

`perform_add_generic_nervous_system_function` has no equivalent guard.

### Impact Explanation

An SNS governance canister ends up with more registered `GenericNervousSystemFunction` entries than `MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS` permits. This violates the invariant enforced at proposal time, can inflate canister heap memory beyond the intended bound, and may cause downstream logic that assumes the cap is respected (e.g., memory-budget calculations, list-functions responses) to behave incorrectly. The corrupted state persists until a corrective governance proposal is executed.

### Likelihood Explanation

Any SNS neuron holder with sufficient stake can submit `AddGenericNervousSystemFunction` proposals. When an SNS is near the cap (a realistic operational state for active SNSes), two neuron holders independently submit proposals with different function IDs. Both proposals pass submission-time validation. Both gather enough votes and are adopted. Both execute in the same heartbeat cycle or across consecutive heartbeats. No special privilege, key compromise, or majority corruption is required — only normal governance participation.

### Recommendation

Add the capacity check inside `perform_add_generic_nervous_system_function`, mirroring the pattern already used in `perform_manage_nervous_system_parameters`:

```rust
fn perform_add_generic_nervous_system_function(
    &mut self,
    nervous_system_function: NervousSystemFunction,
) -> Result<(), GovernanceError> {
    // ... existing checks ...

    // Re-check capacity at execution time (state may have changed since proposal submission).
    let current_count = self.proto.id_to_nervous_system_functions
        .values()
        .filter(|f| !f.is_deletion_marker())
        .count();
    if current_count >= MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            "Reached maximum number of allowed GenericNervousSystemFunctions",
        ));
    }

    self.proto.id_to_nervous_system_functions.insert(id, nervous_system_function);
    Ok(())
}
```

### Proof of Concept

1. SNS has `MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS - 1` registered functions.
2. Neuron A submits `AddGenericNervousSystemFunction` with `id = 1000`. Submission-time validation passes (`len == MAX - 1 < MAX`).
3. Neuron B submits `AddGenericNervousSystemFunction` with `id = 1001`. Submission-time validation passes (same snapshot, `len == MAX - 1 < MAX`).
4. Both proposals gather enough votes and are adopted.
5. Proposal A executes: `perform_add_generic_nervous_system_function` inserts id 1000. Registry now has `MAX` entries.
6. Proposal B executes: `perform_add_generic_nervous_system_function` checks only for duplicate ID (1001 is not a duplicate) and inserts id 1001. Registry now has `MAX + 1` entries — exceeding the intended cap with no error returned. [4](#0-3) [2](#0-1)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1373-1412)
```rust
pub fn validate_and_render_add_generic_nervous_system_function(
    disallowed_target_canister_ids: &HashSet<CanisterId>,
    add: &NervousSystemFunction,
    existing_functions: &BTreeMap<u64, NervousSystemFunction>,
) -> Result<String, String> {
    let validated_function = ValidGenericNervousSystemFunction::try_from(add)?;
    if existing_functions.contains_key(&validated_function.id) {
        return Err(format!(
            "There is already a NervousSystemFunction with id: {}",
            validated_function.id
        ));
    }

    let target_canister_id = validated_function.target_canister_id;
    let validator_canister_id = validated_function.validator_canister_id;

    if disallowed_target_canister_ids.contains(&target_canister_id)
        || disallowed_target_canister_ids.contains(&validator_canister_id)
    {
        return Err("Function targets a reserved canister.".to_string());
    }

    if existing_functions.len() >= MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS {
        return Err("Reached maximum number of allowed GenericNervousSystemFunctions".to_string());
    }

    // This isn't done in ValidGenericNervousSystemFunction::try_from because it's only invalid for new functions, not
    // for existing functions
    if validated_function.topic.is_none() {
        return Err("NervousSystemFunction must have a topic".to_string());
    }

    Ok(format!(
        r"Proposal to add new NervousSystemFunction:

## Function:

{add:#?}"
    ))
}
```

**File:** rs/sns/governance/src/governance.rs (L2247-2298)
```rust
    fn perform_add_generic_nervous_system_function(
        &mut self,
        nervous_system_function: NervousSystemFunction,
    ) -> Result<(), GovernanceError> {
        let id = nervous_system_function.id;

        if nervous_system_function.is_native() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can only add NervousSystemFunction's of \
                                                          GenericNervousSystemFunction function_type",
            ));
        }

        if is_registered_function_id(id, &self.proto.id_to_nervous_system_functions) {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to add NervousSystemFunction. \
                             There is/was already a NervousSystemFunction with id: {id}"
                ),
            ));
        }

        // This validates that it is well-formed, but not the canister targets.
        match ValidGenericNervousSystemFunction::try_from(&nervous_system_function) {
            Ok(valid_function) => {
                let reserved_canisters = self.reserved_canister_targets();
                let target_canister_id = valid_function.target_canister_id;
                let validator_canister_id = valid_function.validator_canister_id;

                if reserved_canisters.contains(&target_canister_id)
                    || reserved_canisters.contains(&validator_canister_id)
                {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        "Cannot add generic nervous system functions that targets sns core canisters, the NNS ledger, or ic00",
                    ));
                }
            }
            Err(msg) => {
                return Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    msg,
                ));
            }
        }

        self.proto
            .id_to_nervous_system_functions
            .insert(id, nervous_system_function);
        Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L2601-2616)
```rust
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
```
