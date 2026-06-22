### Title
SNS `AddGenericNervousSystemFunction` Registers Not-Yet-Deployed Canister Targets Without Code Existence Check - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The `validate_and_render_add_generic_nervous_system_function` function in SNS governance does not verify that the `target_canister_id` or `validator_canister_id` of a `GenericNervousSystemFunction` has a Wasm module installed. An SNS developer with sufficient initial voting power can register a function pointing to an empty (code-free) canister, pass the community voting period during which the target code is unauditable, then deploy malicious code to that canister and execute it via a subsequent governance proposal.

---

### Finding Description

`GenericNervousSystemFunction` is the SNS mechanism that allows governance proposals to call arbitrary external canisters. When a new function is registered via `AddGenericNervousSystemFunction`, the validation path is: [1](#0-0) 

The function is **synchronous** and only validates:
- The `target_canister_id` and `validator_canister_id` are valid principal IDs (format check only)
- They are not in the `disallowed_target_canister_ids` set (SNS core canisters + ic00)
- The function ID is ≥ 1000, name is non-empty, etc. [2](#0-1) 

The `validate_canister_id` helper only checks that the field is populated and converts it to a `CanisterId` — it makes no on-chain call to verify the canister exists or has code installed: [3](#0-2) 

The same gap exists in `perform_add_generic_nervous_system_function`, which explicitly notes it validates form but **not canister targets**: [4](#0-3) 

The IC management canister exposes `module_hash: Option<Vec<u8>>` in `canister_status`, which is `None` when no Wasm is installed: [5](#0-4) 

No check against this field is performed anywhere in the `AddGenericNervousSystemFunction` validation path.

---

### Impact Explanation

The SNS voting period is the community's auditing window. When the `target_canister_id` has no code, voters cannot inspect what will be executed. After the proposal passes:

1. The attacker installs malicious code on the previously-empty `target_canister_id`.
2. The malicious `validator_canister_id` (same or different empty canister) controls the **rendering** of all future `ExecuteGenericNervousSystemFunction` proposals for this function — it can return a benign-looking description for any payload, deceiving voters.
3. When an `ExecuteGenericNervousSystemFunction` proposal is adopted, SNS governance calls `target_canister_id.target_method_name(payload)`: [6](#0-5) 

The malicious target canister executes arbitrary code in the context of an SNS-adopted proposal. If the target canister is also a registered dapp canister (controlled by SNS root), it can be used to drain treasury funds, manipulate ledger state, or call back into other SNS-controlled canisters.

---

### Likelihood Explanation

In early SNS deployments, the founding developer team typically holds a majority of neuron voting power (developer neurons with long dissolve delays). This is structurally analogous to the plasma "maintainer" role. The attacker needs only to:

1. Create a canister (deterministic ID, no code installed) — any principal can do this.
2. Submit an `AddGenericNervousSystemFunction` proposal via a `manage_neuron` ingress call — any neuron holder can do this.
3. Have sufficient voting power (or followee relationships) to adopt the proposal.
4. Install malicious code after adoption.
5. Submit and adopt an `ExecuteGenericNervousSystemFunction` proposal.

The entry path is fully reachable via standard ingress calls to the SNS governance canister. No privileged system access is required beyond the voting power that SNS developers routinely hold at launch.

---

### Recommendation

`validate_and_render_add_generic_nervous_system_function` should be made `async` and should call `canister_status` on the management canister for both `target_canister_id` and `validator_canister_id`, rejecting the proposal if `module_hash` is `None` (i.e., no Wasm is installed). This mirrors the fix applied to the plasma `registerExitGame` (`extcodesize > 0` check) and ensures the community can always audit the target code during the voting period.

---

### Proof of Concept

1. Attacker creates canister `X` (empty, no Wasm installed). `canister_status(X).module_hash == None`.
2. Attacker submits `AddGenericNervousSystemFunction` proposal:
   - `target_canister_id = X`
   - `validator_canister_id = X`
   - `target_method_name = "execute"`
   - `validator_method_name = "validate"`
3. `validate_and_render_add_generic_nervous_system_function` passes — it only checks that `X` is a valid principal and not a reserved canister. [7](#0-6) 
4. Community votes. Canister `X` has no code; voters cannot audit it.
5. Proposal adopted. `X` is now a registered `GenericNervousSystemFunction` target. [8](#0-7) 
6. Attacker installs malicious Wasm on `X` (attacker controls `X`).
7. Attacker submits `ExecuteGenericNervousSystemFunction` proposal with `function_id` pointing to the registered function.
8. During validation, `X.validate(payload)` is called — malicious code returns `Ok("Routine parameter update")`, deceiving voters. [9](#0-8) 
9. Community votes on the benign-looking rendered description. Proposal adopted.
10. SNS governance calls `X.execute(payload)` — malicious code runs with full canister capabilities.

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1240-1252)
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
```

**File:** rs/sns/governance/src/proposal.rs (L1309-1318)
```rust
                // Validate the target_canister_id field.
                let target_canister_id =
                    validate_canister_id("target_canister_id", target_canister_id, &mut defects);

                // Validate the validator_canister_id field.
                let validator_canister_id = validate_canister_id(
                    "validator_canister_id",
                    validator_canister_id,
                    &mut defects,
                );
```

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

**File:** rs/sns/governance/src/governance.rs (L2271-2293)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L2295-2298)
```rust
        self.proto
            .id_to_nervous_system_functions
            .insert(id, nervous_system_function);
        Ok(())
```

**File:** packages/ic-management-canister-types/src/lib.rs (L439-440)
```rust
    /// A SHA256 hash of the module installed on the canister. This is null if the canister is empty.
    pub module_hash: Option<Vec<u8>>,
```
