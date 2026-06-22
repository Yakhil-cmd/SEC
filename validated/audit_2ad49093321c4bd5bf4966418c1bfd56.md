### Title
Missing Anonymous Principal Validation for `fallback_controller_principal_ids` in SNS Swap Initialization - (File: `rs/sns/swap/src/types.rs`)

### Summary
The `validate_principal` helper used to validate each entry in `fallback_controller_principal_ids` during SNS Swap `Init` validation only checks that the string is a parseable `PrincipalId`. It does not reject the anonymous principal (`2vxsx-fae`). Consequently, a `CreateServiceNervousSystem` NNS governance proposal that supplies the anonymous principal as a fallback controller passes all validation layers and is deployed. When the SNS token swap is subsequently aborted, `restore_dapp_controllers` sets the dapp canisters' controllers to the anonymous principal, making them permanently uncontrollable.

### Finding Description
`validate_principal` in `rs/sns/swap/src/types.rs` (lines 28–35) only verifies that the input string can be parsed into a `PrincipalId`:

```rust
pub fn validate_principal(p: &str) -> Result<(), String> {
    let _ = PrincipalId::from_str(p).map_err(|x| { ... })?;
    Ok(())
}
``` [1](#0-0) 

This function is called for every entry in `fallback_controller_principal_ids` inside `Init::validate`:

```rust
for fc in &self.fallback_controller_principal_ids {
    validate_principal(fc)?;
}
``` [2](#0-1) 

The anonymous principal (`2vxsx-fae`) is a syntactically valid `PrincipalId` and passes this check. The higher-level `SnsInitPayload::validate_fallback_controller_principal_ids` in `rs/sns/init/src/lib.rs` performs additional checks (non-empty, count limit, parseability, no duplicates) but also never rejects the anonymous principal: [3](#0-2) 

NNS Governance's `validate_create_service_nervous_system` calls `SnsInitPayload::try_from` followed by `validate_post_execution()`, which in turn calls `validate_fallback_controller_principal_ids()`. Because none of these layers reject the anonymous principal, a proposal with `fallback_controller_principal_ids = ["2vxsx-fae"]` passes all validation and is deployed. [4](#0-3) 

When the swap is aborted, `finalize_inner` calls `restore_dapp_controllers_for_finalize`, which calls `restore_dapp_controllers`: [5](#0-4) 

`restore_dapp_controllers` parses the stored `fallback_controller_principal_ids` and passes them directly to `set_dapp_controllers` on SNS Root: [6](#0-5) 

SNS Root then calls the management canister's `update_settings` to set the anonymous principal as the sole controller of all registered dapp canisters. Since no entity can authenticate as the anonymous principal, those canisters become permanently uncontrollable.

The test fixtures in `rs/sns/integration_tests/src/payment_flow.rs` and `rs/sns/integration_tests/src/swap.rs` confirm that the anonymous principal is accepted without error: [7](#0-6) [8](#0-7) 

### Impact Explanation
If the anonymous principal is accepted as a fallback controller and the SNS token swap is aborted, all dapp canisters registered with the SNS swap canister have their controllers permanently set to the anonymous principal via `set_dapp_controllers`. No one can authenticate as the anonymous principal, so the dapp canisters lose all controllership permanently — they cannot be upgraded, stopped, deleted, or have their settings changed. This is a complete and irreversible loss of control over the dapp canisters.

### Likelihood Explanation
An SNS developer could accidentally supply the anonymous principal as a fallback controller (e.g., as a placeholder during testing that is never replaced). The NNS governance proposal validation does not catch this. The swap canister's own `init` validation also does not catch it. The bug is triggered only if the swap is subsequently aborted, which is a realistic outcome (e.g., minimum participation not reached). Likelihood is low-to-medium: it requires an honest mistake or a malicious actor who tricks a dapp owner into registering with a swap that has this configuration.

### Recommendation
Add an explicit check for the anonymous principal in `validate_principal` (`rs/sns/swap/src/types.rs`) and in `validate_fallback_controller_principal_ids` (`rs/sns/init/src/lib.rs`). For example:

```rust
pub fn validate_principal(p: &str) -> Result<(), String> {
    let pid = PrincipalId::from_str(p).map_err(|x| { ... })?;
    if pid == PrincipalId::new_anonymous() {
        return Err("Anonymous principal is not allowed as a fallback controller".to_string());
    }
    Ok(())
}
```

### Proof of Concept
1. Submit a `CreateServiceNervousSystem` NNS governance proposal with `fallback_controller_principal_ids = ["2vxsx-fae"]` (the anonymous principal's text encoding).
2. Observe that `validate_create_service_nervous_system` → `validate_post_execution` → `validate_fallback_controller_principal_ids` all return `Ok(())`.
3. The SNS is deployed; the swap canister stores the anonymous principal as the fallback controller.
4. The swap reaches its due date without meeting minimum participation; lifecycle transitions to `Aborted`.
5. `finalize` is called; `should_restore_dapp_control()` returns `true`; `restore_dapp_controllers` is invoked.
6. SNS Root calls `update_settings` on all registered dapp canisters, setting `controllers = [anonymous_principal]`.
7. All dapp canisters are now permanently uncontrollable. [1](#0-0) [3](#0-2) [6](#0-5)

### Citations

**File:** rs/sns/swap/src/types.rs (L28-35)
```rust
pub fn validate_principal(p: &str) -> Result<(), String> {
    let _ = PrincipalId::from_str(p).map_err(|x| {
        format!(
            "Couldn't validate PrincipalId. String \"{p}\" could not be converted to PrincipalId: {x}"
        )
    })?;
    Ok(())
}
```

**File:** rs/sns/swap/src/types.rs (L289-294)
```rust
        if self.fallback_controller_principal_ids.is_empty() {
            return Err("at least one fallback controller required".to_string());
        }
        for fc in &self.fallback_controller_principal_ids {
            validate_principal(fc)?;
        }
```

**File:** rs/sns/init/src/lib.rs (L1087-1143)
```rust
    fn validate_fallback_controller_principal_ids(&self) -> Result<(), String> {
        if self.fallback_controller_principal_ids.is_empty() {
            return Err(
                "Error: At least one principal ID must be supplied as a fallback controller \
                 in case the initial token swap fails."
                    .to_string(),
            );
        }

        if self.fallback_controller_principal_ids.len()
            > MAX_FALLBACK_CONTROLLER_PRINCIPAL_IDS_COUNT
        {
            return Err(format!(
                "Error: The number of fallback_controller_principal_ids \
                must be less than {}. Current count is {}",
                MAX_FALLBACK_CONTROLLER_PRINCIPAL_IDS_COUNT,
                self.fallback_controller_principal_ids.len()
            ));
        }

        let (valid_principals, invalid_principals): (Vec<_>, Vec<_>) = self
            .fallback_controller_principal_ids
            .iter()
            .map(|principal_id_string| {
                (
                    principal_id_string,
                    PrincipalId::from_str(principal_id_string),
                )
            })
            .partition(|item| item.1.is_ok());

        if !invalid_principals.is_empty() {
            return Err(format!(
                "Error: One or more fallback_controller_principal_ids is not a valid principal id. \
                The follow principals are invalid: {:?}",
                invalid_principals
                    .into_iter()
                    .map(|pair| pair.0)
                    .collect::<Vec<_>>()
            ));
        }

        // At this point, all principals are valid. Dedupe the values
        let unique_principals: BTreeSet<_> = valid_principals
            .iter()
            .filter_map(|pair| pair.1.clone().ok())
            .collect();

        if unique_principals.len() != valid_principals.len() {
            return Err(
                "Error: Duplicate PrincipalIds found in fallback_controller_principal_ids"
                    .to_string(),
            );
        }

        Ok(())
    }
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

**File:** rs/sns/swap/src/swap.rs (L1354-1382)
```rust
    pub async fn restore_dapp_controllers(
        &self,
        sns_root_client: &mut impl SnsRootClient,
    ) -> Result<Result<SetDappControllersResponse, CanisterCallError>, String> {
        let (controller_principal_ids, errors): (Vec<PrincipalId>, Vec<String>) = self
            .init()?
            .fallback_controller_principal_ids
            .iter()
            .map(|maybe_principal_id| PrincipalId::from_str(maybe_principal_id))
            .partition_map(|result| match result {
                Ok(p) => Either::Left(p),
                Err(msg) => Either::Right(msg.to_string()),
            });

        if !errors.is_empty() {
            return Err(format!(
                "Could not set_dapp_controllers, one or more fallback_controller_principal_ids \
                could not be parsed as a PrincipalId. {:?}",
                errors.join("\n")
            ));
        }

        Ok(sns_root_client
            .set_dapp_controllers(SetDappControllersRequest {
                canister_ids: None,
                controller_principal_ids,
            })
            .await)
    }
```

**File:** rs/sns/swap/src/swap.rs (L1572-1583)
```rust
        if self.should_restore_dapp_control() {
            // Restore controllers of dapp canisters to their original
            // owners (i.e. self.init.fallback_controller_principal_ids).
            finalize_swap_response.set_set_dapp_controllers_result(
                self.restore_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );

            // In the case of returning control of the dapp(s) to the fallback
            // controllers, finalize() need not do any more work, so always return
            // and end execution.
            return finalize_swap_response;
```

**File:** rs/sns/integration_tests/src/payment_flow.rs (L47-48)
```rust
    pub static ref DEFAULT_FALLBACK_CONTROLLER_PRINCIPAL_IDS: Vec<Principal> =
        vec![Principal::anonymous()];
```

**File:** rs/sns/integration_tests/src/swap.rs (L20-20)
```rust
        fallback_controller_principal_ids: vec![Principal::anonymous().to_string()],
```
