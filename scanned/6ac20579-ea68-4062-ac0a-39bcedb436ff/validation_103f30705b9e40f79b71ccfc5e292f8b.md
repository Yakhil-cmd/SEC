### Title
Users Can Remove All Permissions From an SNS Neuron, Permanently Locking Staked Tokens - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister allows any neuron holder to remove all permissions from their own neuron via `RemoveNeuronPermissions`. Once all permissions are removed, no principal can disburse, manage, or recover the neuron, permanently locking the staked SNS tokens. The code itself acknowledges this danger in comments but implements no guard against it.

### Finding Description

The `remove_neuron_permissions` function in `rs/sns/governance/src/governance.rs` processes `RemoveNeuronPermissions` commands without checking whether the operation would leave the neuron with zero total permissions across all principals. [1](#0-0) 

The function delegates to `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs`, which removes the `NeuronPermission` entry entirely when all permission types for a given principal are removed, with no floor check: [2](#0-1) 

The protocol's own documentation acknowledges this is dangerous: [3](#0-2) 

The same warning appears in the generated Rust bindings: [4](#0-3) 

An integration test confirms the operation succeeds and leaves `neuron.permissions.len() == 0`: [5](#0-4) 

### Impact Explanation

Once a neuron's `permissions` list is empty:

- No principal holds `Disburse` (`NeuronPermissionType = 5`) — the staked SNS tokens cannot be withdrawn.
- No principal holds `ManagePrincipals` (`NeuronPermissionType = 2`) — no one can re-add permissions.
- No principal holds `ConfigureDissolveState` (`NeuronPermissionType = 1`) — the dissolve state cannot be changed.

The neuron persists in governance state indefinitely with its staked tokens permanently inaccessible. There is no admin override or recovery path in the SNS governance canister for this condition. The `NeuronPermissionType` enum covers all controllable actions: [6](#0-5) 

### Likelihood Explanation

**Low.** A user must deliberately (or accidentally) call `manage_neuron` with `RemoveNeuronPermissions` targeting all their own permission types. The most plausible scenario is a user attempting to "clean up" or transfer control of a neuron and inadvertently removing all permissions before adding new ones, or a script/integration bug that removes permissions in the wrong order. The operation is reachable by any unprivileged ingress sender who holds a neuron — no special role is required.

### Recommendation

In `remove_neuron_permissions` (or `remove_permissions_for_principal`), add a post-removal check that the neuron still has at least one principal with at least one permission. If the resulting `permissions` list would be empty, return a `GovernanceError` with `ErrorType::PreconditionFailed`. Alternatively, enforce that the `Disburse` permission can never be the last permission removed, ensuring the staked tokens remain recoverable.

### Proof of Concept

1. A user stakes SNS tokens and claims a neuron, receiving all `NeuronPermissionType` permissions (as granted by `neuron_claimer_permissions`).
2. The user calls `manage_neuron` on the SNS governance canister with:
   ```
   ManageNeuron {
     subaccount: <neuron_subaccount>,
     command: RemoveNeuronPermissions {
       principal_id: <user_principal>,
       permissions_to_remove: NeuronPermissionList { permissions: NeuronPermissionType::all() }
     }
   }
   ```
3. `remove_neuron_permissions` at [7](#0-6)  passes all precondition checks (caller has `ManagePrincipals`, principal exists, all permission types are present).
4. `remove_permissions_for_principal` at [8](#0-7)  finds `remaining_permission_types.is_empty()`, removes the `NeuronPermission` entry, and returns `AllPermissionTypesRemoved`.
5. The neuron now has `permissions = []`. No principal can call any `manage_neuron` command on it. The staked tokens are permanently locked.

### Citations

**File:** rs/sns/governance/src/governance.rs (L4645-4716)
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
    }
```

**File:** rs/sns/governance/src/neuron.rs (L782-793)
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
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L2022-2032)
```text
  // Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
  // its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
  // operation as it's possible to remove all permissions for a neuron and no longer be able to modify
  // its state, i.e. disbursing the neuron back into the governance token.
  message RemoveNeuronPermissions {
    // The PrincipalId that the permissions will be revoked from.
    ic_base_types.pb.v1.PrincipalId principal_id = 1;

    // The set of permissions that will be revoked from the PrincipalId.
    NeuronPermissionList permissions_to_remove = 2;
  }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L3091-3094)
```rust
    /// Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
    /// its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
    /// operation as it's possible to remove all permissions for a neuron and no longer be able to modify
    /// its state, i.e. disbursing the neuron back into the governance token.
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L4287-4319)
```rust
pub enum NeuronPermissionType {
    /// Unused, here for PB lint purposes.
    Unspecified = 0,
    /// The principal has permission to configure the neuron's dissolve state. This includes
    /// start dissolving, stop dissolving, and increasing the dissolve delay for the neuron.
    ConfigureDissolveState = 1,
    /// The principal has permission to add other principals to modify the neuron.
    /// The nervous system parameter `NervousSystemParameters::neuron_grantable_permissions`
    /// determines the maximum set of privileges that a principal can grant to another principal in
    /// the given SNS.
    ManagePrincipals = 2,
    /// The principal has permission to submit proposals on behalf of the neuron.
    /// Submitting proposals can change a neuron's stake and thus this
    /// is potentially a balance changing operation.
    SubmitProposal = 3,
    /// The principal has permission to vote and follow other neurons on behalf of the neuron.
    Vote = 4,
    /// The principal has permission to disburse the neuron.
    Disburse = 5,
    /// The principal has permission to split the neuron.
    Split = 6,
    /// The principal has permission to merge the neuron's maturity into
    /// the neuron's stake.
    MergeMaturity = 7,
    /// The principal has permission to disburse the neuron's maturity to a
    /// given ledger account.
    DisburseMaturity = 8,
    /// The principal has permission to stake the neuron's maturity.
    StakeMaturity = 9,
    /// The principal has permission to grant/revoke permission to vote and submit
    /// proposals on behalf of the neuron to other principals.
    ManageVotingPermission = 10,
}
```

**File:** rs/sns/integration_tests/src/neuron.rs (L2212-2265)
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
}
```
