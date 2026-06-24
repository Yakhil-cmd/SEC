### Title
Missing Last-`ManagePrincipals`-Holder Guard in `remove_neuron_permissions` Allows Permanent Loss of Neuron Control - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `remove_neuron_permissions` function in `rs/sns/governance/src/governance.rs` does not check whether the removal operation would leave a neuron with zero principals holding `ManagePrincipals`. A principal with `ManagePrincipals` can remove that permission from themselves (or from the last other holder), permanently rendering the neuron uncontrollable by any user. The staked tokens are then permanently locked in the neuron with no way to disburse, split, or otherwise recover them.

---

### Finding Description

The `remove_neuron_permissions` function in `rs/sns/governance/src/governance.rs` (lines 4659–4716) performs the following checks before removing permissions:

1. The `permissions_to_remove` list is not empty and not oversized.
2. The `principal_id` to remove from is present.
3. The caller has `ManagePrincipals` (or `ManageVotingPermission` for voting-only removals).
4. The target principal actually holds the permissions being removed.

There is **no check** that after the removal, at least one principal still holds `ManagePrincipals` on the neuron.

The `remove_permissions_for_principal` function in `rs/sns/governance/src/neuron.rs` (lines 733–793) simply removes the requested permission types from the target principal's entry and, if all permissions are gone, removes the principal's entry entirely from `self.permissions`. It returns `RemovePermissionsStatus::AllPermissionTypesRemoved` or `SomePermissionTypesRemoved`, but the caller in `governance.rs` only uses this status to update the `principal_to_neuron_ids_index` — it never checks whether `ManagePrincipals` was the last one.

The proto comment at line 2022–2025 of `rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto` explicitly acknowledges this danger:

> "This is a dangerous operation as it's possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token."

Yet no enforcement guard exists in the production code path.

---

### Impact Explanation

A neuron owner who holds `ManagePrincipals` can — by mistake or by being tricked — call `RemoveNeuronPermissions` to remove `ManagePrincipals` from themselves when they are the sole holder of that permission. After this call:

- No principal can call `AddNeuronPermissions` to restore access (requires `ManagePrincipals`).
- No principal can call `Disburse`, `Split`, `DisburseMaturity`, or `StakeMaturity` (requires `Disburse`/`Split`/`DisburseMaturity`/`StakeMaturity` permissions, which also cannot be re-granted).
- The staked governance tokens are permanently locked in the neuron.
- The neuron continues to exist and accumulate maturity but can never be recovered.

This is a **permanent, irreversible loss of user funds** (staked SNS governance tokens) triggered by a single user-initiated transaction.

---

### Likelihood Explanation

The entry path is a standard `manage_neuron` ingress call, reachable by any neuron owner. The scenario is:

1. A user stakes tokens and claims a neuron, receiving `ManagePrincipals` by default (per `REQUIRED_NEURON_CLAIMER_PERMISSIONS` in `rs/sns/governance/src/types.rs` lines 437–445).
2. The user calls `manage_neuron` with `Command::RemoveNeuronPermissions`, targeting themselves, with `ManagePrincipals` in the list.
3. The call succeeds — the integration test `test_neuron_remove_all_permissions_of_self` in `rs/sns/integration_tests/src/neuron.rs` (lines 2212–2264) explicitly demonstrates and asserts this succeeds, leaving `neuron.permissions.len() == 0`.

This is a realistic user mistake (e.g., intending to remove a different permission, or misunderstanding the operation). The code even has a test that confirms the dangerous path works without error.

---

### Recommendation

In `remove_neuron_permissions` (`rs/sns/governance/src/governance.rs`, after line 4705), after calling `remove_permissions_for_principal`, add a guard that checks whether any principal still holds `ManagePrincipals` on the neuron. If none do, return a `GovernanceError` with `ErrorType::PreconditionFailed` and roll back (or perform the check before mutation).

Concretely, before the mutation at line 4700, compute whether the removal would leave zero `ManagePrincipals` holders. If so, reject the operation with an error message such as: `"Cannot remove ManagePrincipals: this would leave the neuron with no principal able to manage it."` An exception can be made for neurons that are intentionally "abandoned" (e.g., Neurons' Fund neurons controlled solely by the NNS governance canister, as detected by `is_neurons_fund_controlled()`).

---

### Proof of Concept

**Attacker-controlled entry path:** Any SNS neuron owner via `manage_neuron` ingress.

```
Step 0:
  USER stakes tokens and claims a neuron.
  Neuron permissions: [USER → {ManagePrincipals, Vote, SubmitProposal}]
  (default neuron_claimer_permissions per rs/sns/governance/src/types.rs:437-445)

Step 1:
  USER calls manage_neuron(
    subaccount = <neuron subaccount>,
    command = RemoveNeuronPermissions {
      principal_id: USER,
      permissions_to_remove: [ManagePrincipals, Vote, SubmitProposal, Disburse, ...]
    }
  )
  
  remove_neuron_permissions() in rs/sns/governance/src/governance.rs:4659
    → check_principal_authorized_to_change_permissions() passes (USER has ManagePrincipals)
    → remove_permissions_for_principal() removes all permissions
    → returns AllPermissionTypesRemoved
    → principal_to_neuron_ids_index updated
    → Ok(()) returned  ← NO GUARD AGAINST ZERO ManagePrincipals HOLDERS

Step 2:
  Neuron permissions: [] (empty)
  USER can no longer call AddNeuronPermissions (requires ManagePrincipals).
  USER can no longer call Disburse (requires Disburse permission).
  Staked tokens are permanently locked.
```

This exact scenario is confirmed by the existing integration test `test_neuron_remove_all_permissions_of_self` at `rs/sns/integration_tests/src/neuron.rs:2212`, which asserts `neuron.permissions.len() == 0` after the call succeeds. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4659-4716)
```rust
    fn remove_neuron_permissions(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
        remove_neuron_permissions: &RemoveNeuronPermissions,
    ) -> Result<(), GovernanceError> {
        let neuron = self.get_neuron_result(neuron_id)?;

        let permissions_to_remove = remove_neuron_permissions
            .permissions_to_remove
            .as_ref()
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "RemoveNeuronPermissions command must provide permissions to remove",
                )
            })?;

        // A simple check to prevent DoS attack with large number of permission changes.
        if permissions_to_remove.permissions.len() > NeuronPermissionType::all().len() {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "RemoveNeuronPermissions command provided more permissions than exist in the system",
            ));
        }

        let principal_id = remove_neuron_permissions
            .principal_id
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "RemoveNeuronPermissions command must provide a PrincipalId to remove permissions from",
                )
            })?;

        neuron.check_principal_authorized_to_change_permissions(
            caller,
            permissions_to_remove.clone(),
        )?;

        // Re-borrow the neuron mutably to update now that the preconditions have been met
        let principal_id_was_removed = self
            .get_neuron_result_mut(neuron_id)?
            .remove_permissions_for_principal(
                principal_id,
                permissions_to_remove.permissions.clone(),
            )?;

        if principal_id_was_removed == RemovePermissionsStatus::AllPermissionTypesRemoved {
            GovernanceProto::remove_neuron_from_principal_in_principal_to_neuron_ids_index(
                &mut self.principal_to_neuron_ids_index,
                neuron_id,
                &principal_id,
            )
        }

        Ok(())
    }
```

**File:** rs/sns/governance/src/neuron.rs (L142-178)
```rust
    /// Returns Ok if the caller has ManagePrincipals, or if the caller has
    /// ManageVotingPermission and the permissions to change relate to voting.
    pub(crate) fn check_principal_authorized_to_change_permissions(
        &self,
        caller: &PrincipalId,
        permissions_to_change: NeuronPermissionList,
    ) -> Result<(), GovernanceError> {
        // If the permissions to change are exclusively voting related,
        // ManagePrincipals or ManageVotingPermission is sufficient.
        // Otherwise, only ManagePrincipals is sufficient.
        let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
            vec![
                NeuronPermissionType::ManagePrincipals,
                NeuronPermissionType::ManageVotingPermission,
            ]
        } else {
            vec![NeuronPermissionType::ManagePrincipals]
        };

        // The caller is authorized if they have any of the sufficient permissions
        let caller_authorized = sufficient_permissions
            .iter()
            .any(|sufficient_permission| self.is_authorized(caller, *sufficient_permission));

        if caller_authorized {
            Ok(())
        } else {
            let caller_permissions = self.permissions_for_principal(caller);
            Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "Caller '{caller:?}' is not authorized to modify permissions {permissions_to_change} for neuron '{}' as it does not have any of {sufficient_permissions:?}. (Caller's permissions are {caller_permissions})",
                    self.id.as_ref().expect("Neuron must have a NeuronId"),
                ),
            ))
        }
    }
```

**File:** rs/sns/governance/src/neuron.rs (L730-793)
```rust
    /// Removes a given permissions from a principalId's `NeuronPermission` for this neuron.
    /// Returns an enum indicating if a `NeuronPermission' is removed due to all of the
    /// principalId's PermissionTypes being removed.
    pub fn remove_permissions_for_principal(
        &mut self,
        principal_id: PrincipalId,
        permission_types_to_remove: Vec<i32>,
    ) -> Result<RemovePermissionsStatus, GovernanceError> {
        // Get the position as it will reduce search time in the future.
        let existing_permission_position = self
            .permissions
            .iter()
            .position(|p| p.principal == Some(principal_id))
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::AccessControlList,
                    format!(
                        "PrincipalId {} does not have any permissions in Neuron {}",
                        principal_id,
                        self.id.as_ref().expect("Neuron must have a NeuronId")
                    ),
                )
            })?;

        let existing_permission = self
            .permissions
            .get_mut(existing_permission_position)
            .expect("Expected permission to exist");

        // Initialize a structure to efficiently remove provided permission_types from
        // existing permission_types.
        let mut remaining_permission_types: HashSet<i32> =
            HashSet::from_iter(existing_permission.permission_type.iter().cloned());

        // Initialize a structure to track if permission_types were present in the existing NeuronPermission
        let mut missing_permissions = HashSet::new();
        for permission_type in &permission_types_to_remove {
            let permission_type_is_present = remaining_permission_types.remove(permission_type);
            if !permission_type_is_present {
                missing_permissions.insert(NeuronPermissionType::try_from(*permission_type).ok());
            }
        }

        if !missing_permissions.is_empty() {
            return Err(GovernanceError::new_with_message(
                ErrorType::AccessControlList,
                format!(
                    "PrincipalId {principal_id} was missing permissions {missing_permissions:?} when removing {permission_types_to_remove:?}"
                ),
            ));
        }

        // If there are no remaining permissions after removing the requested permissions, remove
        // the NeuronPermission entry from the neuron.
        if remaining_permission_types.is_empty() {
            self.permissions.swap_remove(existing_permission_position);
            return Ok(RemovePermissionsStatus::AllPermissionTypesRemoved);
        // If not, update the existing permission with what is left in the remaining permissions.
        } else {
            existing_permission.permission_type = Vec::from_iter(remaining_permission_types);
        }

        Ok(RemovePermissionsStatus::SomePermissionTypesRemoved)
    }
```

**File:** rs/sns/governance/src/types.rs (L437-445)
```rust
    pub const REQUIRED_NEURON_CLAIMER_PERMISSIONS: &'static [NeuronPermissionType] = &[
        // Without this permission, it would be impossible to transfer control
        // of a neuron to a new principal.
        NeuronPermissionType::ManagePrincipals,
        // Without this permission, it would be impossible to vote.
        NeuronPermissionType::Vote,
        // Without this permission, it would be impossible to submit a proposal.
        NeuronPermissionType::SubmitProposal,
    ];
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L2022-2025)
```text
  // Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
  // its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
  // operation as it's possible to remove all permissions for a neuron and no longer be able to modify
  // its state, i.e. disbursing the neuron back into the governance token.
```

**File:** rs/sns/integration_tests/src/neuron.rs (L2212-2264)
```rust
#[test]
fn test_neuron_remove_all_permissions_of_self() {
    local_test_on_sns_subnet(|runtime| async move {
        let user = Sender::from_keypair(&TEST_USER1_KEYPAIR);
        let account_identifier = Account {
            owner: user.get_principal_id().0,
            subaccount: None,
        };
        let alloc = Tokens::from_tokens(1000).unwrap();

        let system_params = NervousSystemParameters {
            neuron_claimer_permissions: Some(NeuronPermissionList {
                permissions: NeuronPermissionType::all(),
            }),
            ..NervousSystemParameters::with_default_values()
        };

        let sns_init_payload = SnsTestsInitPayloadBuilder::new()
            .with_ledger_account(account_identifier, alloc)
            .with_nervous_system_parameters(system_params)
            .build();

        let sns_canisters = SnsCanisters::set_up(&runtime, sns_init_payload).await;

        let neuron_id = sns_canisters.stake_and_claim_neuron(&user, None).await;
        let neuron = sns_canisters.get_neuron(&neuron_id).await;
        let subaccount = neuron.subaccount().expect("Error creating the subaccount");

        // Assert that the Claimer has been granted all permissions
        assert_eq!(neuron.permissions.len(), 1);
        let mut neuron_permission =
            get_neuron_permission_from_neuron(&neuron, &user.get_principal_id());
        // .sort() emits () and needs to be called outside of the assert!
        neuron_permission.permission_type.sort_unstable();
        assert_eq!(
            neuron_permission.permission_type,
            NeuronPermissionType::all(),
        );

        sns_canisters
            .remove_neuron_permissions_or_panic(
                &user,
                &subaccount,
                &user.get_principal_id(),
                NeuronPermissionType::all(),
            )
            .await;

        let neuron = sns_canisters.get_neuron(&neuron_id).await;
        assert_eq!(neuron.permissions.len(), 0);

        Ok(())
    });
```
