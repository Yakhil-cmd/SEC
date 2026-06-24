### Title
Single-Step Removal of All Neuron Permissions Permanently Locks Staked SNS Tokens - (File: rs/sns/governance/src/governance.rs)

### Summary
The `remove_neuron_permissions` function in SNS governance allows any principal holding `ManagePrincipals` permission to remove all permissions from all principals on a neuron in a single ingress transaction, with no two-step confirmation and no guard against leaving the neuron permanently uncontrollable. Once all permissions are removed, the staked SNS tokens are irreversibly locked.

### Finding Description
`remove_neuron_permissions` in `rs/sns/governance/src/governance.rs` processes a `RemoveNeuronPermissions` command submitted via `manage_neuron`. The only authorization check performed is `check_principal_authorized_to_change_permissions`, which verifies the caller holds `ManagePrincipals` (or `ManageVotingPermission` for voting-only changes). There is no guard that prevents:

1. Removing the caller's own `ManagePrincipals` permission.
2. Removing all permissions for all principals on the neuron in successive single-step calls.
3. Leaving the neuron with zero principals having any permission.

The code itself acknowledges the danger in the doc-comment immediately above the function:

> "If all the permissions are removed from the Neuron i.e. by removing all permissions for all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token."

Despite this acknowledgment, no enforcement is added. The authorization check at `check_principal_authorized_to_change_permissions` only validates the caller's current permissions; it does not validate the post-removal state of the neuron. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
Once all permissions are removed from a neuron, no principal can:
- Dissolve or disburse the neuron (recovering staked SNS tokens).
- Vote, follow, or manage the neuron in any way.
- Re-add permissions (since `ManagePrincipals` is required to add permissions, and no one holds it).

The staked SNS tokens are permanently locked inside the neuron with no on-chain recovery path. This is a direct loss-of-funds impact equivalent to the River contract's single-step admin change to a wrong address. [4](#0-3) 

### Likelihood Explanation
- `ManagePrincipals` is a standard permission granted to neuron creators by default in SNS deployments (controlled by `neuron_claimer_permissions` in `NervousSystemParameters`).
- The operation is a normal `manage_neuron` ingress call, reachable by any neuron holder.
- A user can accidentally remove all their own permissions in a single call by passing `NeuronPermissionType::all()` as `permissions_to_remove` for their own `principal_id`.
- A malicious co-holder of a shared neuron who has `ManagePrincipals` can deliberately strip all other principals' permissions, then remove their own, permanently locking the neuron.
- No UI warning, no time-lock, and no on-chain confirmation step exists to prevent this. [5](#0-4) 

### Recommendation
Implement a post-removal invariant check inside `remove_neuron_permissions`: after computing the would-be resulting permission set, reject the operation if it would leave the neuron with no principal holding `ManagePrincipals`. This mirrors the two-step ownership-change pattern recommended in the external report — the current holder must not be able to relinquish the last controlling permission in a single step without a pending-acceptance mechanism or at minimum a safety guard.

Alternatively, require a two-step process: the current `ManagePrincipals` holder proposes a new holder, and the new holder must explicitly accept before the old holder's permissions are removed.

### Proof of Concept
1. Alice creates an SNS neuron; she is granted `ManagePrincipals` and all other permissions by default.
2. Alice submits a `manage_neuron` ingress call with command `RemoveNeuronPermissions { principal_id: alice, permissions_to_remove: NeuronPermissionList::all() }`.
3. `check_principal_authorized_to_change_permissions` passes because Alice currently holds `ManagePrincipals`.
4. `remove_permissions_for_principal` removes all of Alice's permissions. The neuron now has zero principals with any permissions.
5. Alice's staked SNS tokens are permanently locked. No `manage_neuron` call can ever succeed on this neuron again because every operation requires at least one permission, and no principal holds any. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4645-4650)
```rust
    /// Removes a set of permissions for a PrincipalId on an existing Neuron.
    ///
    /// If all the permissions are removed from the Neuron i.e. by removing all permissions for
    /// all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is
    /// possible to remove all permissions for a neuron and no longer be able to modify its
    /// state, i.e. disbursing the neuron back into the governance token.
```

**File:** rs/sns/governance/src/governance.rs (L4659-4715)
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
