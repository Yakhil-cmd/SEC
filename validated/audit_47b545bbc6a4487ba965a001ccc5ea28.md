### Title
SNS Neuron Permanently Locked by Removing All Permissions Without Safeguard — (`File: rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance `remove_neuron_permissions` function allows a caller with `ManagePrincipals` permission to remove all permissions from every principal on a neuron, including themselves, with no check that at least one principal retains control. Once all permissions are stripped, the neuron is permanently inaccessible: its staked tokens cannot be disbursed, it cannot vote, and no recovery path exists.

### Finding Description
`remove_neuron_permissions` in `rs/sns/governance/src/governance.rs` performs only two checks before executing: (1) the caller holds `ManagePrincipals` (or `ManageVotingPermission` for voting-related removals), and (2) the target principal actually holds the permissions being removed. There is no post-condition check that at least one principal retains any permission on the neuron after the operation completes.

The code itself documents the danger:

> "This is a dangerous operation as it is possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token." [1](#0-0) 

The proto definition repeats the same warning: [2](#0-1) 

The low-level `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs` removes the `NeuronPermission` entry entirely when all permission types for a given principal are gone, but it has no visibility into whether other principals still hold permissions on the neuron: [3](#0-2) 

An integration test (`test_neuron_remove_all_permissions_of_self`) explicitly confirms the operation succeeds and leaves `neuron.permissions.len() == 0`: [4](#0-3) 

### Impact Explanation
Once all permissions are removed, no principal can call any permissioned `manage_neuron` command on the neuron. The staked governance tokens are permanently locked inside the neuron's ledger subaccount with no mechanism to disburse them. The neuron also loses all voting power permanently. This is a direct loss of user funds and governance participation rights, analogous to transferring protocol ownership to an uncontrolled address.

**Impact: 5** — permanent, irrecoverable loss of staked tokens.

### Likelihood Explanation
The operation requires the neuron owner (an unprivileged ingress sender) to hold `ManagePrincipals` and to call `manage_neuron` with `RemoveNeuronPermissions` targeting their own principal with all permission types. This can happen by mistake (e.g., intending to remove a hotkey but accidentally targeting the controller), or by a social-engineering scenario where a user is convinced to "clean up" their neuron permissions. No privileged role, key compromise, or subnet majority is required.

**Likelihood: 1** — low probability but entirely reachable by an unprivileged ingress sender.

### Recommendation
Before completing the permission removal, verify that the resulting neuron state still has at least one principal holding `ManagePrincipals` (or the minimum set required to disburse). If the removal would leave the neuron with zero total permissions, reject the operation with a clear error. Concretely, after computing the post-removal permission set, check:

```rust
let remaining_manage_principals = neuron.permissions.iter()
    .any(|p| p.permission_type.contains(&(NeuronPermissionType::ManagePrincipals as i32)));
if !remaining_manage_principals {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Cannot remove permissions: neuron would have no remaining ManagePrincipals holder",
    ));
}
```

### Proof of Concept

**Entry path:** unprivileged ingress sender → SNS Governance canister `manage_neuron` update call.

1. User stakes SNS tokens and claims a neuron. The claimer is granted all permissions including `ManagePrincipals` per `neuron_claimer_permissions`.
2. User sends an ingress `manage_neuron` message with:
   ```
   Command::RemoveNeuronPermissions(RemoveNeuronPermissions {
       principal_id: Some(<user_principal>),
       permissions_to_remove: Some(NeuronPermissionList::all()),
   })
   ```
3. `manage_neuron_internal` dispatches to `remove_neuron_permissions`. [5](#0-4) 
4. The function verifies the caller has `ManagePrincipals` ✓, then calls `remove_permissions_for_principal`, which removes the entire `NeuronPermission` entry because no permission types remain. [6](#0-5) 
5. `neuron.permissions` is now empty. No principal holds any permission. All subsequent `manage_neuron` calls on this neuron fail with `NotAuthorized` because no caller can satisfy any permission check.
6. The staked tokens in the neuron's ledger subaccount are permanently inaccessible.

### Citations

**File:** rs/sns/governance/src/governance.rs (L4645-4715)
```rust
    /// Removes a set of permissions for a PrincipalId on an existing Neuron.
    ///
    /// If all the permissions are removed from the Neuron i.e. by removing all permissions for
    /// all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is
    /// possible to remove all permissions for a neuron and no longer be able to modify its
    /// state, i.e. disbursing the neuron back into the governance token.
    ///
    /// Preconditions:
    /// - the caller has the permission to change a neuron's access control
    ///   (permission `ManagePrincipals`), or the caller has the permission to
    ///   manage voting-related permissions (permission `ManageVotingPermission`)
    ///   and the permissions being removed are voting-related.
    /// - the PrincipalId exists within the neuron's permissions
    /// - the PrincipalId's NeuronPermission contains the permission_types that are to be removed
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
```

**File:** rs/sns/governance/src/governance.rs (L4834-4836)
```rust
            C::RemoveNeuronPermissions(r) => self
                .remove_neuron_permissions(&neuron_id, caller, r)
                .map(|_| ManageNeuronResponse::remove_neuron_permissions_response()),
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L2022-2025)
```text
  // Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
  // its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
  // operation as it's possible to remove all permissions for a neuron and no longer be able to modify
  // its state, i.e. disbursing the neuron back into the governance token.
```

**File:** rs/sns/governance/src/neuron.rs (L782-792)
```rust
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
```

**File:** rs/sns/integration_tests/src/neuron.rs (L2212-2261)
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
```
