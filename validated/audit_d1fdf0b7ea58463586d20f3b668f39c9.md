### Title
SNS Neuron Ownership Can Be Fully Renounced, Permanently Locking Staked Tokens - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister's `remove_neuron_permissions` function allows a neuron owner to remove all permissions from all principals on a neuron, leaving it in a permanently uncontrollable state. Because `disburse_neuron` requires the `NeuronPermissionType::Disburse` permission, a neuron with an empty permission list can never be disbursed. The staked SNS tokens held in the neuron's ledger subaccount are permanently locked with no recovery path. The code itself acknowledges this is a "dangerous operation" but does not prevent it.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `remove_neuron_permissions` function allows any caller with `ManagePrincipals` permission to remove all permissions from any principal on a neuron, including themselves:

```rust
// rs/sns/governance/src/governance.rs:4645-4716
fn remove_neuron_permissions(
    &mut self,
    neuron_id: &NeuronId,
    caller: &PrincipalId,
    remove_neuron_permissions: &RemoveNeuronPermissions,
) -> Result<(), GovernanceError> {
    ...
    neuron.check_principal_authorized_to_change_permissions(
        caller,
        permissions_to_remove.clone(),
    )?;
    // No check that at least one principal retains Disburse permission
    let principal_id_was_removed = self
        .get_neuron_result_mut(neuron_id)?
        .remove_permissions_for_principal(
            principal_id,
            permissions_to_remove.permissions.clone(),
        )?;
    ...
    Ok(())
}
```

The underlying `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs` will happily remove the last permission entry, leaving `neuron.permissions` empty:

```rust
// rs/sns/governance/src/neuron.rs:784-786
if remaining_permission_types.is_empty() {
    self.permissions.swap_remove(existing_permission_position);
    return Ok(RemovePermissionsStatus::AllPermissionTypesRemoved);
```

The `disburse_neuron` function in `rs/sns/governance/src/governance.rs` requires `NeuronPermissionType::Disburse`:

```rust
// rs/sns/governance/src/governance.rs:1127
neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;
```

`check_authorized` in `rs/sns/governance/src/neuron.rs` iterates `self.permissions` and returns `NotAuthorized` if no entry matches. With an empty permissions list, every caller is rejected for every operation — including `Disburse`, `ConfigureDissolveState`, `ManagePrincipals`, and `DisburseMaturity`.

The proto comment itself acknowledges the risk:
> "This is a dangerous operation as it's possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token."

The integration test `test_neuron_remove_all_permissions_of_self` in `rs/sns/integration_tests/src/neuron.rs` confirms this is reachable and succeeds, resulting in `neuron.permissions.len() == 0`.

### Impact Explanation
A neuron owner who calls `manage_neuron` with `Command::RemoveNeuronPermissions` removing all their own permissions (including `Disburse`) permanently loses the ability to recover staked SNS tokens. The neuron continues to exist in governance state, its subaccount on the SNS ledger continues to hold the staked tokens, but no principal can ever call `disburse_neuron` or `disburse_maturity` successfully. The tokens are permanently locked in the ledger subaccount with no on-chain recovery mechanism. This is a **governance authorization bug / ledger conservation bug**: staked tokens become permanently unrecoverable.

### Likelihood Explanation
This is reachable by any SNS neuron owner via a standard ingress `manage_neuron` call — no privileged access is required. The attacker-controlled entry path is: unprivileged user → `manage_neuron` ingress → `Command::RemoveNeuronPermissions` with `NeuronPermissionType::all()` targeting their own principal. The operation succeeds unconditionally as confirmed by the existing integration test. The likelihood is **medium**: it requires the neuron owner to either make a mistake or be socially engineered into calling this, but the operation is fully self-service and irreversible.

### Recommendation
Add a post-removal invariant check in `remove_neuron_permissions` that ensures at least one principal retains `NeuronPermissionType::Disburse` (and ideally `ManagePrincipals`) after the removal. If the resulting neuron would have an empty permissions list or no principal with `Disburse`, the call should return an error. Alternatively, override the behavior to prevent removing the last `Disburse`-capable principal from a neuron that still holds a non-zero stake.

### Proof of Concept
1. Stake SNS tokens and claim a neuron, receiving all permissions including `Disburse` and `ManagePrincipals`.
2. Call `manage_neuron` with:
   ```
   Command::RemoveNeuronPermissions(RemoveNeuronPermissions {
       principal_id: Some(<own_principal>),
       permissions_to_remove: Some(NeuronPermissionList {
           permissions: NeuronPermissionType::all(),
       }),
   })
   ```
3. The call succeeds. `neuron.permissions` is now empty (confirmed by `test_neuron_remove_all_permissions_of_self` in `rs/sns/integration_tests/src/neuron.rs`).
4. Attempt to call `manage_neuron` with `Command::Disburse(...)` — returns `NotAuthorized`.
5. The staked tokens in the neuron's ledger subaccount are permanently inaccessible. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1119-1127)
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

**File:** rs/sns/governance/src/neuron.rs (L104-122)
```rust
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
```

**File:** rs/sns/governance/src/neuron.rs (L782-786)
```rust
        // If there are no remaining permissions after removing the requested permissions, remove
        // the NeuronPermission entry from the neuron.
        if remaining_permission_types.is_empty() {
            self.permissions.swap_remove(existing_permission_position);
            return Ok(RemovePermissionsStatus::AllPermissionTypesRemoved);
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L2022-2025)
```text
  // Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
  // its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
  // operation as it's possible to remove all permissions for a neuron and no longer be able to modify
  // its state, i.e. disbursing the neuron back into the governance token.
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
