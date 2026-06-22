### Title
SNS Governance `add_neuron_permissions` Allows Privilege Escalation Beyond Caller's Own Permission Set - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

In the SNS Governance canister, a principal holding only `ManagePrincipals` on a neuron can grant any permission within `neuron_grantable_permissions` to an arbitrary third party — including permissions the caller does not themselves hold. The third party can then revoke the original caller's permissions, gaining exclusive full control of the neuron and its staked tokens. This is a direct analog of the Escher M-13 finding: a privileged role can transfer authority exceeding its own to an unprivileged party, defeating the purpose of granular access control.

---

### Finding Description

`add_neuron_permissions` in `rs/sns/governance/src/governance.rs` enforces two checks before granting permissions to a target principal:

1. The caller holds `ManagePrincipals` (or `ManageVotingPermission` for voting-only changes).
2. The permissions to add are within the system-level `neuron_grantable_permissions`. [1](#0-0) 

There is **no check** that the permissions being granted are a subset of the caller's own permissions. The function `check_principal_authorized_to_change_permissions` only verifies the caller has `ManagePrincipals` — it does not compare the requested grant against what the caller actually holds. [2](#0-1) 

For all SNS instances deployed via the standard `SnsInitPayload` path, `neuron_grantable_permissions` is set to **all permissions**: [3](#0-2) 

The default `neuron_claimer_permissions` grants `ManagePrincipals`, `Vote`, and `SubmitProposal` to every neuron claimer: [4](#0-3) 

This means every standard neuron holder — who has `ManagePrincipals` by default — can grant `Disburse`, `Split`, `ConfigureDissolveState`, and `ManagePrincipals` to an arbitrary third party, even though the caller does not hold those permissions themselves. The third party can then remove the original caller's permissions entirely, gaining exclusive control of the neuron and its staked tokens.

The `remove_neuron_permissions` function has the same structural gap: a caller with `ManagePrincipals` can remove any permission from any principal, including permissions the caller does not hold. [5](#0-4) 

---

### Impact Explanation

A neuron claimer (who holds `ManagePrincipals`, `Vote`, `SubmitProposal` by default) can:

1. Grant an attacker-controlled principal all permissions including `Disburse`, `Split`, `ManagePrincipals`.
2. The attacker then removes the original claimer's `ManagePrincipals`.
3. The attacker is now the sole controller of the neuron and can disburse its staked governance tokens to an arbitrary ledger account.

This results in **direct financial loss** (staked tokens disbursed), **governance manipulation** (attacker can submit and vote on proposals using the neuron's voting power), and **permanent lockout** of the original neuron owner. The neuron is not deleted when all permissions are removed from the original owner — it persists under attacker control. [6](#0-5) 

---

### Likelihood Explanation

- **High**: Every SNS deployed via the standard `SnsInitPayload` path sets `neuron_grantable_permissions = all_permissions`. This covers all production SNS deployments.
- Every neuron claimer receives `ManagePrincipals` by default, so no special setup is required for the attacker.
- The attack requires only a standard ingress `update` call to `manage_neuron` on the SNS governance canister — no privileged access, no threshold corruption, no social engineering.
- The attack is a two-step sequence of two ordinary `manage_neuron` calls, both of which are publicly documented and reachable by any ingress sender. [7](#0-6) 

---

### Recommendation

In `add_neuron_permissions`, after verifying the caller has `ManagePrincipals`, add a check that the permissions being granted are a subset of the caller's own permissions:

```rust
// Ensure the caller cannot grant permissions they do not themselves hold.
let caller_permission_types: HashSet<i32> = neuron
    .permissions
    .iter()
    .find(|p| p.principal == Some(*caller))
    .map(|p| p.permission_type.iter().cloned().collect())
    .unwrap_or_default();

for permission in &permissions_to_add.permissions {
    if !caller_permission_types.contains(permission) {
        return Err(GovernanceError::new_with_message(
            ErrorType::NotAuthorized,
            "Cannot grant permissions that the caller does not hold",
        ));
    }
}
```

Apply the same check symmetrically in `remove_neuron_permissions` if desired (a caller should only be able to remove permissions they themselves hold, to prevent a `ManagePrincipals`-only holder from stripping a `Disburse`-holding principal of that permission). [8](#0-7) 

---

### Proof of Concept

**Setup**: SNS deployed via standard path; `neuron_grantable_permissions = all`. Alice claims a neuron and receives `[ManagePrincipals, Vote, SubmitProposal]`.

**Step 1** — Alice (attacker or colluding party) calls `manage_neuron` as ingress:
```
ManageNeuron {
  subaccount: <alice_neuron_subaccount>,
  command: AddNeuronPermissions {
    principal_id: Bob,
    permissions_to_add: [ManagePrincipals, Disburse, Split, ConfigureDissolveState,
                         MergeMaturity, DisburseMaturity, StakeMaturity, Vote,
                         SubmitProposal, ManageVotingPermission]
  }
}
```
This succeeds: Alice has `ManagePrincipals`, all permissions are in `neuron_grantable_permissions`. No subset check is performed. [9](#0-8) 

**Step 2** — Bob calls `manage_neuron`:
```
ManageNeuron {
  subaccount: <alice_neuron_subaccount>,
  command: RemoveNeuronPermissions {
    principal_id: Alice,
    permissions_to_remove: [ManagePrincipals, Vote, SubmitProposal]
  }
}
```
This succeeds: Bob has `ManagePrincipals`.

**Result**: Bob is now the sole principal on Alice's neuron with all permissions. Bob calls `Disburse` to transfer the staked tokens to his own ledger account. Alice has no remaining permissions and cannot recover the neuron. [10](#0-9)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4570-4634)
```rust
    fn add_neuron_permissions(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
        add_neuron_permissions: &AddNeuronPermissions,
    ) -> Result<(), GovernanceError> {
        let neuron = self.get_neuron_result(neuron_id)?;

        let permissions_to_add = add_neuron_permissions
            .permissions_to_add
            .as_ref()
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "AddNeuronPermissions command must provide permissions to add",
                )
            })?;

        // A simple check to prevent DoS attack with large number of permission changes.
        if permissions_to_add.permissions.len() > NeuronPermissionType::all().len() {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command provided more permissions than exist in the system",
            ));
        }

        neuron
            .check_principal_authorized_to_change_permissions(caller, permissions_to_add.clone())?;

        self.nervous_system_parameters_or_panic()
            .check_permissions_are_grantable(permissions_to_add)?;

        let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
            )
        })?;

        let existing_permissions = neuron
            .permissions
            .iter()
            .find(|permission| permission.principal == Some(principal_id));

        let max_number_of_principals_per_neuron = self
            .nervous_system_parameters_or_panic()
            .max_number_of_principals_per_neuron
            .expect("NervousSystemParameters.max_number_of_principals_per_neuron must be present");

        // If the PrincipalId does not already exist in the neuron, make sure it can be added
        if existing_permissions.is_none()
            && neuron.permissions.len() == max_number_of_principals_per_neuron as usize
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Cannot add permission to neuron. Max \
                    number of principals reached {max_number_of_principals_per_neuron}"
                ),
            ));
        }

        // Re-borrow the neuron mutably to update now that the preconditions have been met
        self.get_neuron_result_mut(neuron_id)?
            .add_permissions_for_principal(principal_id, permissions_to_add.permissions.clone());
```

**File:** rs/sns/governance/src/governance.rs (L4645-4651)
```rust
    /// Removes a set of permissions for a PrincipalId on an existing Neuron.
    ///
    /// If all the permissions are removed from the Neuron i.e. by removing all permissions for
    /// all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is
    /// possible to remove all permissions for a neuron and no longer be able to modify its
    /// state, i.e. disbursing the neuron back into the governance token.
    ///
```

**File:** rs/sns/governance/src/governance.rs (L4694-4697)
```rust
        neuron.check_principal_authorized_to_change_permissions(
            caller,
            permissions_to_remove.clone(),
        )?;
```

**File:** rs/sns/governance/src/governance.rs (L4831-4836)
```rust
            C::AddNeuronPermissions(p) => self
                .add_neuron_permissions(&neuron_id, caller, p)
                .map(|_| ManageNeuronResponse::add_neuron_permissions_response()),
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

**File:** rs/sns/init/src/lib.rs (L812-815)
```rust
        NervousSystemParameters {
            neuron_claimer_permissions: Some(all_permissions.clone()),
            neuron_grantable_permissions: Some(all_permissions),
            transaction_fee_e8s,
```

**File:** rs/sns/governance/api_helpers/src/lib.rs (L15-19)
```rust
pub const DEFAULT_NEURON_CLAIMER_PERMISSIONS: &[NeuronPermissionType] = &[
    NeuronPermissionType::ManagePrincipals,
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
];
```
