### Title
SNS Neuron Stake Permanently Locked When All Permissions Are Removed - (`rs/sns/governance/src/governance.rs`)

### Summary
The SNS Governance canister allows any principal holding `ManagePrincipals` permission on a neuron — including the neuron owner themselves — to remove all `NeuronPermission` entries from that neuron. Once all permissions are cleared, the staked SNS tokens held in the neuron's ledger subaccount become permanently inaccessible, because every sensitive operation (including `Disburse`) requires a permission check that will always fail on a permission-less neuron. There is no recovery path.

### Finding Description

The `remove_neuron_permissions` function in `rs/sns/governance/src/governance.rs` allows a caller with `ManagePrincipals` (or `ManageVotingPermission` for voting-only permissions) to remove any set of permissions from any principal on a neuron, including removing all permissions from the last remaining principal: [1](#0-0) 

The function itself documents the danger but imposes no safeguard:

> "If all the permissions are removed from the Neuron i.e. by removing all permissions for all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token."

The underlying `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs` will `swap_remove` the last `NeuronPermission` entry when all permission types for a principal are removed, leaving `neuron.permissions` as an empty `Vec`: [2](#0-1) 

After this, `disburse_neuron` in `rs/sns/governance/src/governance.rs` calls `check_authorized` with `NeuronPermissionType::Disburse`: [3](#0-2) 

`is_authorized` iterates `neuron.permissions` looking for the caller's principal. With an empty list, it always returns `false`: [4](#0-3) 

The result is a `NotAuthorized` error on every disburse attempt, with no escape hatch. The staked tokens remain in the neuron's ledger subaccount indefinitely.

The integration test `test_neuron_remove_all_permissions_of_self` in `rs/sns/integration_tests/src/neuron.rs` explicitly confirms that a user can reduce `neuron.permissions.len()` to `0`: [5](#0-4) 

The proto definition for `RemoveNeuronPermissions` also acknowledges the danger without enforcing a guard: [6](#0-5) 

### Impact Explanation

Staked SNS governance tokens held in a neuron's ledger subaccount become permanently inaccessible. The neuron record persists in governance state but no principal can call `Disburse`, `DisburseMaturity`, `Split`, `Configure`, or any other neuron management command. The tokens are effectively burned without any burn event. For SNS projects with significant token value locked in neurons, this represents a direct, irreversible loss of user funds.

### Likelihood Explanation

There are two realistic paths:

1. **Self-inflicted (any neuron owner):** A user who holds `ManagePrincipals` on their own neuron — which is granted by default via `neuron_claimer_permissions` in many SNS deployments — can accidentally remove all their own permissions. The `RemoveNeuronPermissions` command accepts `NeuronPermissionType::all()` as a valid input and the system will execute it without warning. [7](#0-6) 

2. **Malicious co-principal:** If Alice granted `ManagePrincipals` to Bob (e.g., a multisig or a hot key), Bob can call `RemoveNeuronPermissions` targeting Alice's principal with all permission types, stripping Alice of `Disburse` and all other permissions. Bob can then also remove his own permissions, leaving the neuron permanently orphaned. This is the direct analog to the Connext M-10 finding. [8](#0-7) 

### Recommendation

Add a guard in `remove_neuron_permissions` (or in `remove_permissions_for_principal`) that prevents the operation from leaving a neuron with zero total permissions across all principals. Specifically, before committing the removal, verify that at least one principal will retain `Disburse` permission after the operation completes. If the removal would result in a neuron with no `Disburse`-capable principal, reject the transaction with a descriptive error. Alternatively, implement a governance-level recovery mechanism (e.g., allowing the SNS root or governance canister to forcibly disburse a permission-less neuron to its original staking subaccount).

### Proof of Concept

1. Alice stakes SNS tokens and claims a neuron. She receives all permissions including `ManagePrincipals` and `Disburse` via `neuron_claimer_permissions`.
2. Alice calls `manage_neuron` with `Command::RemoveNeuronPermissions { principal_id: alice, permissions_to_remove: NeuronPermissionList::all() }`.
3. `remove_neuron_permissions` passes `check_principal_authorized_to_change_permissions` (Alice has `ManagePrincipals`).
4. `remove_permissions_for_principal` removes the last `NeuronPermission` entry; `neuron.permissions` is now empty.
5. Alice's neuron dissolves (time passes or she had already started dissolving).
6. Alice calls `manage_neuron` with `Command::Disburse { ... }`.
7. `disburse_neuron` calls `neuron.check_authorized(&alice, NeuronPermissionType::Disburse)`.
8. `is_authorized` finds no entry for Alice in the empty `permissions` vec and returns `false`.
9. The call returns `GovernanceError { error_type: NotAuthorized }`.
10. Alice's staked tokens are permanently locked with no recovery path. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1119-1136)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        // First check authorized
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;

        // Check that the neuron is dissolved.
        let state = neuron.state(self.env.now());
        if state != NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {id} is NOT dissolved. It is in state {state:?}"),
            ));
        }
```

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

**File:** rs/sns/governance/src/neuron.rs (L124-140)
```rust
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

**File:** rs/sns/governance/src/neuron.rs (L142-177)
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
```

**File:** rs/sns/governance/src/neuron.rs (L782-786)
```rust
        // If there are no remaining permissions after removing the requested permissions, remove
        // the NeuronPermission entry from the neuron.
        if remaining_permission_types.is_empty() {
            self.permissions.swap_remove(existing_permission_position);
            return Ok(RemovePermissionsStatus::AllPermissionTypesRemoved);
```

**File:** rs/sns/integration_tests/src/neuron.rs (L2251-2261)
```rust
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

**File:** rs/sns/governance/api_helpers/src/lib.rs (L15-19)
```rust
pub const DEFAULT_NEURON_CLAIMER_PERMISSIONS: &[NeuronPermissionType] = &[
    NeuronPermissionType::ManagePrincipals,
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
];
```
