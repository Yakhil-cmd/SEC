### Title
Deleted Registered Dapp Canister Becomes Permanently Unremovable and Can Block SNS Swap Finalization - (`rs/sns/root/src/lib.rs`)

### Summary

`SnsRootCanister::set_dapp_controllers` contains a panicking pre-flight check that traps the SNS Root canister if `canister_status` fails for any registered dapp canister (e.g., because it was deleted after running out of cycles) or if SNS Root is no longer a controller. Because the only removal path for a registered dapp canister goes through `set_dapp_controllers`, a deleted dapp canister becomes permanently stuck in SNS Root's registry with no force-removal escape hatch. When the swap canister calls `set_dapp_controllers` with `canister_ids: None` (all registered dapps) during swap finalization, a single deleted dapp canister causes the entire finalization to panic and fail.

### Finding Description

`SnsRootCanister::set_dapp_controllers` performs a pre-flight check before changing controllers. For every canister in the target list it calls `management_canister_client.canister_status(...)`. If that call returns an error (e.g., the canister has been deleted), the code unconditionally panics: [1](#0-0) 

The same function is the only code path through which a dapp canister can be removed from SNS Root's `dapp_canister_ids` list. The `DeregisterDappCanisters` SNS governance proposal encodes a `SetDappControllersRequest` and calls `set_dapp_controllers` on Root: [2](#0-1) 

There is no force-removal function for dapp canisters analogous to `clean_up_failed_register_extension` for extensions: [3](#0-2) 

During SNS swap finalization, the swap canister calls `set_dapp_controllers` with `canister_ids: None`, which expands to the full `dapp_canister_ids` list: [4](#0-3) 

The `None` branch in `set_dapp_controllers` collects all registered dapp canister IDs and feeds them into the panicking pre-flight loop: [5](#0-4) 

The TODO comments in the code explicitly acknowledge this is a known defect (NNS1-1993) that should return an error instead of panicking, but the fix has not been applied: [6](#0-5) 

### Impact Explanation

1. **Permanent unremovability**: A registered dapp canister that runs out of cycles and is deleted by the IC becomes permanently stuck in SNS Root's `dapp_canister_ids`. Every `DeregisterDappCanisters` proposal targeting it panics and fails. There is no governance-accessible force-removal path.

2. **Swap finalization freeze**: The swap canister's `take_sole_control_of_dapp_controllers` call uses `canister_ids: None`, causing the pre-flight loop to iterate over every registered dapp canister. A single deleted canister causes the entire swap finalization to panic, permanently blocking the SNS from completing its token sale and distributing tokens to participants.

3. **Cascading governance failure**: Because `DeregisterDappCanisters` proposals always fail for the stuck canister, the SNS community has no on-chain mechanism to recover without an NNS-level intervention.

### Likelihood Explanation

Dapp canisters registered with an SNS can run out of cycles through ordinary operation (no malicious intent required). The IC automatically deletes canisters whose cycle balance reaches zero. A dapp canister developer who controls the canister's code can also deliberately drain cycles. Once deleted, the canister is irrecoverable through the existing SNS governance API. The registration limit of 100 dapp+extension canisters means a single deleted canister can occupy a slot and block finalization indefinitely. [7](#0-6) 

### Recommendation

Replace the two `panic!` / `assert!` calls in the pre-flight loop of `set_dapp_controllers` with graceful error returns (as the existing TODO NNS1-1993 already prescribes). Additionally, add a force-removal function for dapp canisters (analogous to `clean_up_failed_register_extension`) that SNS Governance can call to evict a canister from `dapp_canister_ids` without requiring a successful `canister_status` call on the target canister. [8](#0-7) 

### Proof of Concept

1. An SNS is created and a dapp canister `D` is registered via a `RegisterDappCanisters` proposal. SNS Root is the sole controller of `D`.
2. `D` runs out of cycles; the IC deletes it.
3. The SNS community submits a `DeregisterDappCanisters` proposal listing `D`.
4. SNS Governance calls `Root::set_dapp_controllers({canister_ids: Some([D]), controller_principal_ids: [new_owner]})`.
5. The pre-flight loop calls `management_canister.canister_status(D)`, which returns an error because `D` no longer exists.
6. `set_dapp_controllers` panics; the proposal execution fails; `D` remains in `dapp_canister_ids` forever.
7. The SNS swap canister later calls `Root::set_dapp_controllers({canister_ids: None, ...})` during finalization.
8. The pre-flight loop again hits `D`, panics, and swap finalization is permanently blocked. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/root/src/lib.rs (L607-658)
```rust
    pub async fn clean_up_failed_register_extension(
        self_ref: &'static LocalKey<RefCell<Self>>,
        management_canister_client: &impl ManagementCanisterClient,
        request: CleanUpFailedRegisterExtensionRequest,
    ) -> CleanUpFailedRegisterExtensionResponse {
        let main = async || -> Result<(), CanisterCallError> {
            // Unpack request.
            let CleanUpFailedRegisterExtensionRequest {
                canister_id: extension_canister_id,
            } = request;
            let Some(extension_canister_id) = extension_canister_id else {
                return Err(CanisterCallError {
                    code: Some(RejectCode::CanisterReject as i32),
                    description: "Request lacks canister_id.".to_string(),
                });
            };

            // Remove extension_canister_id from self.extensions.
            //
            // (This might result in no actual changes. In that case, we still
            // power through with the rest of this method.)
            self_ref.with_borrow_mut(|state| {
                let Some(extensions) = state.extensions.as_mut() else {
                    return;
                };

                extensions
                    .extension_canister_ids
                    .retain(|prior_extension_canister_id| {
                        prior_extension_canister_id != &extension_canister_id
                    });
            });

            // Prepare to call stop_canister and delete_canister (by wrapping
            // extension_canister_id in a couple extra layers).
            let extension_canister_id =
                CanisterIdRecord::from(CanisterId::unchecked_from_principal(extension_canister_id));

            // Prepare to delete the canister by stopping it first.
            management_canister_client
                .stop_canister(extension_canister_id)
                .await?;

            // Delete the canister.
            management_canister_client
                .delete_canister(extension_canister_id)
                .await?;

            Ok(())
        };

        CleanUpFailedRegisterExtensionResponse::from(main().await)
```

**File:** rs/sns/root/src/lib.rs (L674-678)
```rust
        if canisters_registered_count >= DAPP_AND_EXTENSION_CANISTER_REGISTRATION_LIMIT {
            Err(format!(
                "Canister registration limit of {DAPP_AND_EXTENSION_CANISTER_REGISTRATION_LIMIT} was reached. No more canisters can be \
                 registered until a current dapp canister or extension is deregistered."
            ))?;
```

**File:** rs/sns/root/src/lib.rs (L776-790)
```rust
        // Grab a snapshot of canisters to operate on.
        let dapp_canister_ids = match &request.canister_ids {
            Some(canister_ids) => canister_ids.canister_ids.clone(),
            // If no canister list is specified, we take all the canisters controlled by root.
            None => {
                let is_authorized_to_set_all_controllers =
                    self_ref.with(|self_ref| caller == self_ref.borrow().swap_canister_id());
                if is_authorized_to_set_all_controllers {
                    self_ref.with(|self_ref| self_ref.borrow().dapp_canister_ids.clone())
                } else {
                    // TODO(NNS1-1993): Remove this panic and return an error type instead.
                    panic!("Only the swap canister is authorized to set all dapp controllers")
                }
            }
        };
```

**File:** rs/sns/root/src/lib.rs (L792-826)
```rust
        // A pre-flight check: Assert that we still control all canisters
        // referenced in dapp_canister_ids. This way, we minimize that chance of
        // failing half way through controller changes, since changing the
        // controllers of many canisters cannot be done atomically.
        for dapp_canister_id in &dapp_canister_ids {
            let dapp_canister_id = CanisterId::try_from(*dapp_canister_id).unwrap_or_else(|err| {
                panic!(
                    "Unable to convert principal ID ({dapp_canister_id}) of a dapp into a \
                     canister ID: {err:#?}"
                )
            });
            let canister_status = match management_canister_client
                .canister_status(dapp_canister_id.into())
                .await
            {
                Err(_) => {
                    // TODO(NNS1-1993): Remove this panic and return an error type instead.
                    panic!(
                        "Could not get the status of canister: {dapp_canister_id}.  Root may not be a controller."
                    )
                }
                Ok(status) => status,
            };
            let is_controllee = canister_status.controllers().contains(&own_canister_id);

            // TODO(NNS1-1993): Remove this assertion and return an error type instead.
            assert!(
                is_controllee,
                "Operation aborted due to an error; no changes have been made: \
                 Unable to determine whether this canister (SNS root) is the controller \
                 of a registered dapp canister ({dapp_canister_id}). This may be due to \
                 the canister having been deleted, which may be due to it running out \
                 of cycles."
            );
        }
```

**File:** rs/sns/governance/src/governance.rs (L2422-2448)
```rust
    async fn perform_deregister_dapp_canisters(
        &self,
        deregister_dapp_canisters: DeregisterDappCanisters,
    ) -> Result<(), GovernanceError> {
        let payload = candid::Encode!(&SetDappControllersRequest::from(
            deregister_dapp_canisters.clone()
        ))
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Could not encode SetDappControllersRequest: {err:?}"),
            )
        })?;
        self.env
            .call_canister(
                self.proto.root_canister_id_or_panic(),
                "set_dapp_controllers",
                payload,
            )
            .await
            // Convert to return type.
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Canister method call failed: {err:?}"),
                )
            })
```

**File:** rs/sns/swap/src/swap.rs (L1386-1397)
```rust
    pub async fn take_sole_control_of_dapp_controllers(
        &self,
        sns_root_client: &mut impl SnsRootClient,
    ) -> Result<Result<SetDappControllersResponse, CanisterCallError>, String> {
        let sns_root_principal_id = self.init()?.sns_root()?.get();
        Ok(sns_root_client
            .set_dapp_controllers(SetDappControllersRequest {
                canister_ids: None,
                controller_principal_ids: vec![sns_root_principal_id],
            })
            .await)
    }
```

**File:** rs/sns/root/canister/canister.rs (L384-395)
```rust
#[update]
async fn set_dapp_controllers(request: SetDappControllersRequest) -> SetDappControllersResponse {
    log!(INFO, "set_dapp_controllers");
    SnsRootCanister::set_dapp_controllers(
        &STATE,
        &ManagementCanisterClientImpl::<CanisterRuntime>::new(None),
        PrincipalId(ic_cdk::api::id()),
        PrincipalId(ic_cdk::api::caller()),
        &request,
    )
    .await
}
```
