### Title
Missing Principal-Type Validation for `target_canister_id` and `validator_canister_id` in SNS `AddGenericNervousSystemFunction` - (File: `rs/sns/governance/src/proposal.rs`)

### Summary

The SNS governance `validate_canister_id` helper used when validating `AddGenericNervousSystemFunction` proposals calls `CanisterId::unchecked_from_principal` without verifying that the supplied `PrincipalId` is actually a canister principal (opaque, 10-byte, byte-8 = `0x01`). Any `PrincipalId` — including a self-authenticating user key or the anonymous principal — is silently accepted as a valid `target_canister_id` or `validator_canister_id`. Once such a function is registered through an adopted proposal, every subsequent `ExecuteGenericNervousSystemFunction` proposal that references it will permanently fail at the validator-call step, rendering the registered function permanently broken and wasting governance cycles.

### Finding Description

`validate_canister_id` in `rs/sns/governance/src/proposal.rs` is the sole gate that converts the caller-supplied `PrincipalId` fields into `CanisterId` values during `AddGenericNervousSystemFunction` proposal validation:

```rust
fn validate_canister_id(
    field_name: &str,
    canister_id: &Option<PrincipalId>,
    defects: &mut Vec<String>,
) -> Option<CanisterId> {
    match canister_id {
        None => {
            defects.push(format!("{field_name} field was not populated."));
            None
        }
        Some(canister_id) => Some(CanisterId::unchecked_from_principal(*canister_id)),
    }
}
``` [1](#0-0) 

`CanisterId::unchecked_from_principal` is explicitly documented as performing **no validation**:

```rust
/// Converts WITHOUT any validation.
///
/// If you want validation, use try_from_principal_id. Do NOT use
/// CanisterId::try_from, because it lies: it does not actually return Err
/// when the input is invalid.
pub const fn unchecked_from_principal(principal_id: PrincipalId) -> Self {
    Self(principal_id)
}
``` [2](#0-1) 

The validated function is then checked only against a reserved-canister blocklist (SNS core canisters, NNS ledger, `ic00`). A user principal is not in that list and passes freely:

```rust
if reserved_canisters.contains(&target_canister_id)
    || reserved_canisters.contains(&validator_canister_id)
{
    return Err(...);
}
``` [3](#0-2) 

The reserved-canister list itself:

```rust
pub fn reserved_canister_targets(&self) -> Vec<CanisterId> {
    vec![
        self.env.canister_id(),
        self.proto.root_canister_id_or_panic(),
        self.proto.ledger_canister_id_or_panic(),
        self.proto.swap_canister_id_or_panic(),
        NNS_LEDGER_CANISTER_ID,
        CanisterId::ic_00(),
    ]
}
``` [4](#0-3) 

The correct validator is `CanisterId::try_from_principal_id`, which enforces opaque class, 10-byte length, and byte-8 = `0x01`:

```rust
pub fn try_from_principal_id(principal_id: PrincipalId) -> Result<Self, CanisterIdError> {
    if principal_id.class() != Ok(PrincipalIdClass::Opaque) { ... }
    if raw.len() != 10 { ... }
    if raw[8] != 0x01 { ... }
    Ok(CanisterId(principal_id))
}
``` [5](#0-4) 

### Impact Explanation

Once an `AddGenericNervousSystemFunction` proposal carrying a non-canister `validator_canister_id` is adopted, every subsequent `ExecuteGenericNervousSystemFunction` proposal referencing that function ID calls `perform_execute_generic_nervous_system_function_validate_and_render_call`, which issues a cross-canister call to the non-canister principal:

```rust
let result = env
    .call_canister(
        valid_function.validator_canister_id,
        &valid_function.validator_method,
        call.payload,
    )
    .await;
``` [6](#0-5) 

The call will always be rejected by the IC (no canister exists at that principal), causing every `ExecuteGenericNervousSystemFunction` proposal for that function to fail at validation. The registered function is permanently broken — it can only be removed by a separate `RemoveGenericNervousSystemFunction` governance proposal. Similarly, a non-canister `target_canister_id` causes every adopted execution proposal to fail at `perform_execute_generic_nervous_system_function_call`, wasting governance cycles and leaving proposals in a failed state. [7](#0-6) 

### Likelihood Explanation

Any SNS neuron holder can submit an `AddGenericNervousSystemFunction` proposal. The error can occur accidentally (a developer pastes their own self-authenticating principal instead of a canister ID) or deliberately (a malicious neuron holder crafts a proposal that looks legitimate but uses a user principal). The validation code gives no error or warning when a non-canister principal is supplied, so the mistake is invisible at proposal-submission time. The proposal must be adopted by a majority vote, but SNS communities routinely adopt governance-function proposals without deep technical review of the raw principal bytes.

### Recommendation

Replace `CanisterId::unchecked_from_principal` with `CanisterId::try_from_principal_id` inside `validate_canister_id` and propagate the error as a defect:

```rust
fn validate_canister_id(
    field_name: &str,
    canister_id: &Option<PrincipalId>,
    defects: &mut Vec<String>,
) -> Option<CanisterId> {
    match canister_id {
        None => {
            defects.push(format!("{field_name} field was not populated."));
            None
        }
        Some(canister_id) => {
            match CanisterId::try_from_principal_id(*canister_id) {
                Ok(id) => Some(id),
                Err(e) => {
                    defects.push(format!(
                        "{field_name} is not a valid canister ID: {e}"
                    ));
                    None
                }
            }
        }
    }
}
```

Apply the same fix to `validate_and_render_register_dapp_canisters` and `validate_and_render_deregister_dapp_canisters`, which also use `CanisterId::unchecked_from_principal` on caller-supplied principal IDs. [8](#0-7) [9](#0-8) 

### Proof of Concept

1. An SNS neuron holder submits an `AddGenericNervousSystemFunction` proposal:
   ```
   target_canister_id    = <any self-authenticating user principal>
   target_method_name    = "execute"
   validator_canister_id = <any self-authenticating user principal>
   validator_method_name = "validate"
   topic                 = ApplicationBusinessLogic
   id                    = 1000
   ```
2. `validate_canister_id` at line 1250 calls `CanisterId::unchecked_from_principal` — no error is raised.
3. The reserved-canister check at line 2278 passes because a user principal is not in the SNS core list.
4. The proposal is adopted and the function is stored in `id_to_nervous_system_functions`.
5. Any neuron holder now submits `ExecuteGenericNervousSystemFunction { function_id: 1000, payload: ... }`.
6. `validate_and_render_execute_nervous_system_function` calls `perform_execute_generic_nervous_system_function_validate_and_render_call`, which issues a cross-canister call to the user principal.
7. The IC rejects the call (destination is not a canister); the proposal is rejected with an error.
8. Step 5–7 repeats forever — the function is permanently broken. [10](#0-9)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1238-1252)
```rust
/// Validates a given canister id and adds a defect to a given list of defects if the there was no
/// canister id given or if it was invalid.
fn validate_canister_id(
    field_name: &str,
    canister_id: &Option<PrincipalId>,
    defects: &mut Vec<String>,
) -> Option<CanisterId> {
    match canister_id {
        None => {
            defects.push(format!("{field_name} field was not populated."));
            None
        }
        Some(canister_id) => Some(CanisterId::unchecked_from_principal(*canister_id)),
    }
}
```

**File:** rs/sns/governance/src/proposal.rs (L1431-1455)
```rust
/// Validates and renders a proposal with action ExecuteNervousSystemFunction.
/// This retrieves the nervous system function's validator method and calls it.
pub async fn validate_and_render_execute_nervous_system_function(
    env: &dyn Environment,
    execute: &ExecuteGenericNervousSystemFunction,
    existing_functions: &BTreeMap<u64, NervousSystemFunction>,
) -> Result<String, String> {
    let id = execute.function_id;
    match existing_functions.get(&execute.function_id) {
        None => Err(format!("There is no NervousSystemFunction with id: {id}")),
        Some(function) => {
            // Make sure this isn't a NervousSystemFunction which has been deleted.
            if function == &*NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER {
                Err(format!("There is no NervousSystemFunction with id: {id}"))
            } else {
                // To validate the proposal we try and call the validation method,
                // which should produce a payload rendering if the proposal is valid
                // or an error if it isn't.
                let rendering =
                    perform_execute_generic_nervous_system_function_validate_and_render_call(
                        env,
                        function.clone(),
                        execute.clone(),
                    )
                    .await?;
```

**File:** rs/sns/governance/src/proposal.rs (L1614-1618)
```rust
    let canisters_to_register = register_dapp_canisters
        .canister_ids
        .iter()
        .map(|id| CanisterId::unchecked_from_principal(*id))
        .collect::<HashSet<CanisterId>>();
```

**File:** rs/sns/governance/src/proposal.rs (L1673-1677)
```rust
    let canisters_to_deregister = deregister_dapp_canisters
        .canister_ids
        .iter()
        .map(|id| CanisterId::unchecked_from_principal(*id))
        .collect::<HashSet<CanisterId>>();
```

**File:** rs/types/base_types/src/canister_id.rs (L72-79)
```rust
    /// Converts WITHOUT any validation.
    ///
    /// If you want validation, use try_from_principal_id. Do NOT use
    /// CanisterId::try_from, because it lies: it does not actually return Err
    /// when the input is invalid.
    pub const fn unchecked_from_principal(principal_id: PrincipalId) -> Self {
        Self(principal_id)
    }
```

**File:** rs/types/base_types/src/canister_id.rs (L115-145)
```rust
    pub fn try_from_principal_id(principal_id: PrincipalId) -> Result<Self, CanisterIdError> {
        // Must be opaque.
        if principal_id.class() != Ok(PrincipalIdClass::Opaque) {
            return Err(CanisterIdError::InvalidPrincipalId(format!(
                "Principal ID {} is of class {:?} (not Opaque).",
                principal_id,
                principal_id.class(),
            )));
        }

        // Must be of length 10.
        let raw = principal_id.as_slice();
        if raw.len() != 10 {
            return Err(CanisterIdError::InvalidPrincipalId(format!(
                "Principal ID {} consists of {} bytes (not 10).",
                principal_id,
                raw.len(),
            )));
        }

        // Byte 8 (penultimate) must be 0x01.
        if raw[8] != 0x01 {
            return Err(CanisterIdError::InvalidPrincipalId(format!(
                "Byte 8 (9th) of Principal ID {} is not 0x01: {}",
                principal_id,
                hex::encode(raw),
            )));
        }

        Ok(CanisterId(principal_id))
    }
```

**File:** rs/sns/governance/src/governance.rs (L808-817)
```rust
    pub fn reserved_canister_targets(&self) -> Vec<CanisterId> {
        vec![
            self.env.canister_id(),
            self.proto.root_canister_id_or_panic(),
            self.proto.ledger_canister_id_or_panic(),
            self.proto.swap_canister_id_or_panic(),
            NNS_LEDGER_CANISTER_ID,
            CanisterId::ic_00(),
        ]
    }
```

**File:** rs/sns/governance/src/governance.rs (L2278-2285)
```rust
                if reserved_canisters.contains(&target_canister_id)
                    || reserved_canisters.contains(&validator_canister_id)
                {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        "Cannot add generic nervous system functions that targets sns core canisters, the NNS ledger, or ic00",
                    ));
                }
```

**File:** rs/sns/governance/src/canister_control.rs (L248-254)
```rust
    let result = env
        .call_canister(
            valid_function.validator_canister_id,
            &valid_function.validator_method,
            call.payload,
        )
        .await;
```

**File:** rs/sns/governance/src/canister_control.rs (L277-315)
```rust
/// Executes a generic nervous system function (i.e., a non-native SNS proposal).
pub async fn perform_execute_generic_nervous_system_function_call(
    env: &dyn Environment,
    function: NervousSystemFunction,
    call: ExecuteGenericNervousSystemFunction,
) -> Result<(), GovernanceError> {
    // Get the canister id and the method against which we execute the proposal.
    let valid_function = ValidGenericNervousSystemFunction::try_from(&function)
        .map_err(|e| GovernanceError::new_with_message(ErrorType::InvalidProposal, e))?;

    let result = env
        .call_canister(
            valid_function.target_canister_id,
            &valid_function.target_method,
            call.payload,
        )
        .await;

    // Convert result.
    match result {
        Err(err) => Err(GovernanceError::new_with_message(
            ErrorType::External,
            format!("Canister method call to execute proposal failed: {err:?}"),
        )),

        Ok(_reply) => {
            // TODO: Do something with reply. E.g. store it in the proposal,
            // and/or deserialize it so that we can detect whether there was an
            // application-level error, as opposed to a communication
            // error. Detecting application error could be done as follows:
            //
            //   candid::!Decode(&reply, Result<String, String>)
            //
            // This could then be converted into a Result<(), GovernanceError>.
            // For now, any reply is considered a success.
            Ok(())
        }
    }
}
```
