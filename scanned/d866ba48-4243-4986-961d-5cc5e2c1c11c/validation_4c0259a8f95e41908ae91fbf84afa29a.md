### Title
SNS Neuron Permissions Can Be Fully Renounced, Permanently Locking Staked Tokens - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister allows any neuron holder with `ManagePrincipals` permission to irrevocably remove all permissions from every principal on their neuron — including their own — via `RemoveNeuronPermissions`. Once all permissions are stripped, the neuron's staked SNS tokens are permanently locked: no principal can disburse, vote, split, or manage the neuron in any way. The neuron is not deleted and the tokens are not returned. The codebase explicitly acknowledges this danger in comments but provides no guard against it.

### Finding Description
The `remove_neuron_permissions` function in `rs/sns/governance/src/governance.rs` processes the `ManageNeuron::RemoveNeuronPermissions` command. It checks that the caller holds `ManagePrincipals` (or `ManageVotingPermission` for voting-only changes), then unconditionally removes the requested permissions from the target principal. [1](#0-0) 

There is no guard preventing the removal of the last `ManagePrincipals` entry, nor any guard preventing the neuron from ending up with zero permissions across all principals. The underlying `remove_permissions_for_principal` function in `rs/sns/governance/src/neuron.rs` simply removes the entry when all permission types are gone: [2](#0-1) 

The protocol itself confirms this is reachable: the integration test `test_neuron_remove_all_permissions_of_self` in `rs/sns/integration_tests/src/neuron.rs` explicitly exercises the path and asserts `neuron.permissions.len() == 0` as the expected outcome. [3](#0-2) 

The `RemoveNeuronPermissions` command is exposed as a standard `manage_neuron` ingress call, reachable by any neuron holder: [4](#0-3) 

### Impact Explanation
Once all permissions are removed from a neuron, every privileged operation on it becomes permanently impossible. Specifically, `disburse_neuron` requires `NeuronPermissionType::Disburse`: [5](#0-4) 

With no permissions remaining, `check_authorized` will always return `NotAuthorized` for any caller. The neuron's staked SNS tokens — held in the neuron's ledger subaccount — are permanently inaccessible. The neuron record persists in governance state but can never be acted upon. This constitutes permanent, unrecoverable loss of user funds (staked governance tokens). The proto comment for `RemoveNeuronPermissions` acknowledges this directly: [6](#0-5) 

### Likelihood Explanation
The trigger requires only that the neuron holder (or any principal they have granted `ManagePrincipals` to) submit a standard `manage_neuron` ingress message. No privileged access, no admin keys, no governance majority, and no external dependencies are needed. The scenario most likely arises from user error (self-inflicted), but a malicious co-holder granted `ManagePrincipals` could also weaponize it against the neuron owner. Likelihood: **2/10** (matches the original report's rating — low probability but entirely reachable without any privilege).

### Recommendation
Add a validation step inside `remove_neuron_permissions` (or inside `remove_permissions_for_principal`) that checks whether the operation would leave the neuron with zero principals holding `ManagePrincipals`. If so, reject the operation with an `InvalidCommand` error. Specifically, after computing the post-removal permission set, verify that at least one principal retains `ManagePrincipals`. This mirrors the standard pattern of requiring at least one owner/admin before renouncing control.

### Proof of Concept
1. Alice stakes SNS tokens and claims a neuron. She is granted all permissions including `ManagePrincipals` and `Disburse` via `neuron_claimer_permissions`.
2. Alice (or a co-holder she granted `ManagePrincipals` to) submits a `manage_neuron` ingress call with command `RemoveNeuronPermissions { principal_id: Alice, permissions_to_remove: NeuronPermissionList::all() }`.
3. `remove_neuron_permissions` at line 4659 passes the `check_principal_authorized_to_change_permissions` check because Alice still holds `ManagePrincipals` at the time of the check (pre-removal).
4. `remove_permissions_for_principal` removes all of Alice's permissions; `remaining_permission_types` is empty, so the `NeuronPermission` entry is `swap_remove`d from the neuron.
5. The neuron now has `permissions.len() == 0`.
6. Any subsequent call to `disburse_neuron`, `split_neuron`, `configure_neuron`, or any other neuron operation fails with `NotAuthorized` for every caller, permanently.
7. Alice's staked SNS tokens remain locked in the neuron's ledger subaccount with no recovery path. [7](#0-6) [8](#0-7)

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

**File:** rs/sns/governance/src/governance.rs (L4834-4836)
```rust
            C::RemoveNeuronPermissions(r) => self
                .remove_neuron_permissions(&neuron_id, caller, r)
                .map(|_| ManageNeuronResponse::remove_neuron_permissions_response()),
```

**File:** rs/sns/governance/src/neuron.rs (L144-178)
```rust
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L2022-2025)
```text
  // Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
  // its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
  // operation as it's possible to remove all permissions for a neuron and no longer be able to modify
  // its state, i.e. disbursing the neuron back into the governance token.
```
