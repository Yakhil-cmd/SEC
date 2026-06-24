### Title
SNS Neuron Permanently Locked After All Permissions Self-Removed — No Recovery Mechanism - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS Governance `RemoveNeuronPermissions` command allows any neuron holder to remove all permissions from their own neuron, including the last `ManagePrincipals` and `Disburse` permissions. Once all permissions are stripped, the neuron's staked SNS tokens are permanently inaccessible — no principal can disburse, configure, or recover the neuron. There is no guard preventing this self-invalidation, and no recovery path exists short of an SNS upgrade.

### Finding Description
The `remove_neuron_permissions` function in `rs/sns/governance/src/governance.rs` processes `RemoveNeuronPermissions` commands submitted by any ingress caller who holds `ManagePrincipals` (or `ManageVotingPermission` for voting-only permissions) on a neuron. [1](#0-0) 

The function delegates to `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs`: [2](#0-1) 

When the last permission type for the last principal is removed, the neuron's `permissions` list becomes empty and the entry is dropped: [3](#0-2) 

There is **no guard** preventing this. The code itself acknowledges the danger in the proto definition: [4](#0-3) 

And in the Rust API types: [5](#0-4) 

The integration test `test_neuron_remove_all_permissions_of_self` confirms this is reachable and succeeds: [6](#0-5) 

After all permissions are removed, every subsequent `manage_neuron` command fails authorization. For example, `disburse_neuron` requires `NeuronPermissionType::Disburse`: [7](#0-6) 

And `check_authorized` will always return `NotAuthorized` because the permissions list is empty: [8](#0-7) 

### Impact Explanation
Once all permissions are removed from an SNS neuron, the staked SNS governance tokens locked in that neuron's subaccount on the ledger are permanently inaccessible. No principal can call `Disburse`, `DisburseMaturity`, `Configure` (to start dissolving), `Split`, or `AddNeuronPermissions` (which also requires `ManagePrincipals`). The neuron exists in the governance state forever with a non-zero `cached_neuron_stake_e8s` but zero permissions — a permanent ledger conservation violation where tokens are locked with no recovery path available to any user. Recovery would require an SNS upgrade proposal, which itself requires other neurons with voting power to pass.

### Likelihood Explanation
The trigger is a single `manage_neuron` ingress call from the neuron's own controller. It can happen accidentally (a user intending to remove a hotkey removes their last permission) or deliberately (a user who loses their key first removes all permissions, making the neuron unrecoverable). The `REQUIRED_NEURON_CLAIMER_PERMISSIONS` constant requires `ManagePrincipals` at claim time: [9](#0-8) 

But nothing prevents removing it afterward. The scenario is realistic for any SNS deployment where a user rotates keys or makes an operational mistake.

### Recommendation
Add a guard in `remove_neuron_permissions` (or in `remove_permissions_for_principal`) that rejects any operation that would leave the neuron with zero total permissions across all principals. Alternatively, require that at least one principal retains `ManagePrincipals` and `Disburse` at all times. The check should be analogous to the fix suggested in the external report: once the "reporter" (the last permission holder) is about to be invalidated, the operation should be blocked or a fallback recovery mechanism should be provided. [10](#0-9) 

### Proof of Concept
1. Stake SNS tokens and claim a neuron. The claimer receives `ManagePrincipals`, `Vote`, and `SubmitProposal` by default (per `REQUIRED_NEURON_CLAIMER_PERMISSIONS`).
2. Call `manage_neuron` with `RemoveNeuronPermissions { principal_id: <self>, permissions_to_remove: NeuronPermissionList::all() }`.
3. The call succeeds — confirmed by `test_neuron_remove_all_permissions_of_self`.
4. Attempt any subsequent `manage_neuron` command (e.g., `Disburse`, `Configure/StartDissolving`, `AddNeuronPermissions`).
5. All calls return `NotAuthorized` because `neuron.permissions` is empty.
6. The staked tokens in the neuron's ledger subaccount are permanently locked with no on-chain recovery path.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1125-1127)
```rust
        // First check authorized
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;
```

**File:** rs/sns/governance/src/governance.rs (L4645-4650)
```rust
    /// Removes a set of permissions for a PrincipalId on an existing Neuron.
    ///
    /// If all the permissions are removed from the Neuron i.e. by removing all permissions for
    /// all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is
    /// possible to remove all permissions for a neuron and no longer be able to modify its
    /// state, i.e. disbursing the neuron back into the governance token.
```

**File:** rs/sns/governance/src/governance.rs (L4694-4715)
```rust
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

**File:** rs/sns/governance/src/neuron.rs (L102-140)
```rust
    /// Checks whether a given principal has the permission to perform a certain action on
    /// the neuron.
    pub(crate) fn check_authorized(
        &self,
        principal: &PrincipalId,
        permission: NeuronPermissionType,
    ) -> Result<(), GovernanceError> {
        if !self.is_authorized(principal, permission) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "Caller '{:?}' is not authorized to perform action: '{:?}' on neuron '{}'.",
                    principal,
                    permission,
                    self.id.as_ref().expect("Neuron must have a NeuronId"),
                ),
            ));
        }

        Ok(())
    }

    /// Returns true if the principalId has the permission to act on this neuron (i.e., self).
    pub(crate) fn is_authorized(
        &self,
        principal: &PrincipalId,
        permission: NeuronPermissionType,
    ) -> bool {
        let found_neuron_permission = self
            .permissions
            .iter()
            .find(|neuron_permission| neuron_permission.principal == Some(*principal));

        if let Some(p) = found_neuron_permission {
            return p.permission_type.contains(&(permission as i32));
        }

        false
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L2022-2025)
```text
  // Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
  // its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
  // operation as it's possible to remove all permissions for a neuron and no longer be able to modify
  // its state, i.e. disbursing the neuron back into the governance token.
```

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L2096-2099)
```rust
    /// Remove a set of permissions from the Neuron for the given PrincipalId. If a PrincipalId has all of
    /// its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
    /// operation as its possible to remove all permissions for a neuron and no longer be able to modify
    /// it's state, i.e. disbursing the neuron back into the governance token.
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
