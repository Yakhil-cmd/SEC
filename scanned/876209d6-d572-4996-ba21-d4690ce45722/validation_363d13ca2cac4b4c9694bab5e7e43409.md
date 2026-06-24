### Title
One-Step Irreversible Dapp Canister Controller Transfer Without New-Controller Confirmation - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS `DeregisterDappCanisters` proposal action immediately and permanently transfers control of registered dapp canisters to caller-supplied `new_controllers` principal IDs in a single atomic step. There is no two-step confirmation requiring the new controllers to accept the transfer. If the `new_controllers` field contains a wrong or unreachable principal ID (e.g., a typo, a burned address, or a miscopied principal), the dapp canisters are permanently and irrecoverably lost to the SNS.

### Finding Description
When an SNS `DeregisterDappCanisters` proposal passes, `perform_deregister_dapp_canisters()` is called in SNS Governance. It immediately encodes the caller-supplied `new_controllers` into a `SetDappControllersRequest` and calls `set_dapp_controllers` on SNS Root with no intermediate confirmation step. [1](#0-0) 

SNS Root's `set_dapp_controllers()` then immediately calls `update_settings` on the IC management canister for each dapp canister, atomically replacing the controller list with the supplied `new_controllers`: [2](#0-1) 

The `DeregisterDappCanisters` struct carries `canister_ids` (canisters to deregister) and `new_controllers` (the replacement controllers), both of which are arbitrary caller-supplied principal IDs: [3](#0-2) 

Validation at proposal submission time only checks that `new_controllers` is non-empty and that `canister_ids` does not include SNS framework canisters. It does **not** verify that the new controllers are reachable, valid, or have consented to accept control: [4](#0-3) 

The conversion from `DeregisterDappCanisters` to `SetDappControllersRequest` passes `new_controllers` directly as `controller_principal_ids` with no further validation: [5](#0-4) 

### Impact Explanation
Once a `DeregisterDappCanisters` proposal executes successfully, the dapp canisters are immediately removed from SNS Root's `dapp_canister_ids` list and their IC-level controller list is replaced with the supplied `new_controllers`. If those principals are wrong (burned, mistyped, or non-existent), the SNS permanently loses all ability to manage those canisters. There is no on-chain recovery path: the SNS Root is no longer a controller and cannot re-register or reclaim the canisters. The impact is permanent, irreversible loss of SNS governance over production dapp canisters. [6](#0-5) 

### Likelihood Explanation
Any SNS neuron holder can submit a `DeregisterDappCanisters` proposal. While the proposal requires higher voting thresholds (67% of exercised voting power, 20% of total), voters review a human-readable text rendering of the proposal that lists principal IDs as opaque strings. A single-character typo in a 27-character principal ID is easy to miss during community review. The SNS governance canister exposes `make_proposal` as an ingress endpoint reachable by any unprivileged principal with sufficient neuron stake. [7](#0-6) 

### Recommendation
Implement a two-step controller transfer for `DeregisterDappCanisters`:
1. **Propose step**: The SNS governance proposal records the intended `new_controllers` but does not immediately apply the change. Instead, it stores a pending transfer with a time-bounded acceptance window.
2. **Accept step**: Each principal in `new_controllers` must call an acceptance endpoint (e.g., `accept_dapp_canister_control`) within the window. Only after all new controllers have accepted does SNS Root call `update_settings` to finalize the transfer.

At minimum, add on-chain validation that each principal in `new_controllers` is a known, reachable canister or user principal before executing the transfer.

### Proof of Concept
1. An SNS neuron holder submits a `DeregisterDappCanisters` proposal with `new_controllers: [<typo'd principal>]` and `canister_ids: [<production dapp>]`.
2. The SNS community votes to approve (the rendered proposal shows the principal as an opaque string; the typo goes unnoticed).
3. `perform_deregister_dapp_canisters()` is called, which calls `set_dapp_controllers` on SNS Root.
4. SNS Root calls `update_settings` on the IC management canister, replacing the dapp's controller list with the typo'd principal.
5. SNS Root removes the dapp from `dapp_canister_ids`.
6. The dapp canister is now controlled exclusively by an unreachable principal. The SNS has no recovery path. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2422-2440)
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
```

**File:** rs/sns/root/src/lib.rs (L762-774)
```rust
    pub async fn set_dapp_controllers<'a>(
        self_ref: &'static LocalKey<RefCell<Self>>,
        management_canister_client: &'a impl ManagementCanisterClient,
        own_canister_id: PrincipalId,
        caller: PrincipalId,
        request: &'a SetDappControllersRequest,
    ) -> SetDappControllersResponse {
        let is_authorized = self_ref.with(|self_ref| {
            caller == self_ref.borrow().swap_canister_id()
                || caller == self_ref.borrow().governance_canister_id()
        });
        // TODO(NNS1-1993): Remove this assertion and return an error type instead.
        assert!(is_authorized, "Caller ({caller}) is not authorized.");
```

**File:** rs/sns/root/src/lib.rs (L839-850)
```rust
            let request = UpdateSettings {
                canister_id: *dapp_canister_id,
                settings: CanisterSettings {
                    controllers: Some(request.controller_principal_ids.clone()),
                    ..Default::default()
                },
                sender_canister_version: management_canister_client.canister_version(),
            };

            // Perform the call.
            let update_result: Result<(), _> =
                management_canister_client.update_settings(request).await;
```

**File:** rs/sns/root/src/lib.rs (L872-879)
```rust
            // If necessary, remove dapp_canister_id from self_ref.
            if !still_controlled_by_this_canister {
                self_ref.with(|self_ref| {
                    swap_remove_if(&mut self_ref.borrow_mut().dapp_canister_ids, |element| {
                        element == dapp_canister_id
                    })
                });
            }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L859-866)
```rust
pub struct DeregisterDappCanisters {
    /// The canister IDs to be deregistered (i.e. removed from the management of the SNS).
    #[prost(message, repeated, tag = "1")]
    pub canister_ids: ::prost::alloc::vec::Vec<::ic_base_types::PrincipalId>,
    /// The new controllers for the deregistered canisters.
    #[prost(message, repeated, tag = "2")]
    pub new_controllers: ::prost::alloc::vec::Vec<::ic_base_types::PrincipalId>,
}
```

**File:** rs/sns/governance/src/proposal.rs (L1669-1671)
```rust
    if deregister_dapp_canisters.new_controllers.is_empty() {
        return Err("DeregisterDappControllers must specify the new controllers".to_string());
    }
```

**File:** rs/sns/governance/src/types.rs (L2585-2593)
```rust
impl From<DeregisterDappCanisters> for SetDappControllersRequest {
    fn from(deregister_dapp_canisters: DeregisterDappCanisters) -> SetDappControllersRequest {
        SetDappControllersRequest {
            canister_ids: Some(CanisterIds {
                canister_ids: deregister_dapp_canisters.canister_ids,
            }),
            controller_principal_ids: deregister_dapp_canisters.new_controllers,
        }
    }
```

**File:** rs/sns/governance/tests/governance.rs (L3488-3503)
```rust
    assert!(
        proposal_data.minimum_yes_proportion_of_exercised.unwrap()
            > NervousSystemParameters::DEFAULT_MINIMUM_YES_PROPORTION_OF_EXERCISED_VOTING_POWER
    );
    assert_eq!(
        proposal_data.minimum_yes_proportion_of_exercised.unwrap(),
        Percentage::from_basis_points(6700)
    );
    assert!(
        proposal_data.minimum_yes_proportion_of_total.unwrap()
            > NervousSystemParameters::DEFAULT_MINIMUM_YES_PROPORTION_OF_TOTAL_VOTING_POWER
    );
    assert_eq!(
        proposal_data.minimum_yes_proportion_of_total.unwrap(),
        Percentage::from_basis_points(2000)
    );
```
