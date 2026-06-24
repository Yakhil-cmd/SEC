### Title
Missing Stake Check Before Removing All Neuron Permissions Allows Permanent Stake Lockout — (File: `rs/sns/governance/src/governance.rs`)

### Summary

The `remove_neuron_permissions` function in SNS governance allows any caller with `ManagePrincipals` permission to remove all permissions from all principals on a neuron without checking whether the neuron still holds staked tokens. If all permissions are removed, the neuron's stake becomes permanently inaccessible — no principal can disburse, split, or otherwise recover the locked tokens. This is the direct IC analog of the reported pattern: a privileged removal operation that ignores remaining invested state.

### Finding Description

`remove_neuron_permissions` in `rs/sns/governance/src/governance.rs` removes a set of `NeuronPermissionType` entries for a given `PrincipalId` on a neuron. The function's own documentation explicitly warns:

> "If all the permissions are removed from the Neuron i.e. by removing all permissions for all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token." [1](#0-0) 

The function performs no check on the neuron's cached stake (`cached_neuron_stake_e8s`) or maturity before allowing the removal to proceed. It only verifies that the caller holds `ManagePrincipals` (or `ManageVotingPermission` for voting-related permissions): [2](#0-1) 

After the removal, the neuron remains in the `neurons` map with its full stake intact, but with no principal authorized to call `disburse`, `split`, `merge_maturity`, or any other state-changing operation. The stake is silently stranded — analogous to the reported pattern where an asset is removed from a strategy while its invested balance is left unaccounted.

The validation path for `RemoveGenericNervousSystemFunction` similarly does not check for open `ExecuteGenericNervousSystemFunction` proposals referencing the same function ID: [3](#0-2) [4](#0-3) 

### Impact Explanation

A malicious canister that has been granted `ManagePrincipals` permission on a neuron (a common pattern for automated voting or neuron management services) can:

1. Call `manage_neuron` → `RemoveNeuronPermissions` to strip the neuron owner's permissions.
2. Call `manage_neuron` → `RemoveNeuronPermissions` again to strip its own permissions.
3. The neuron's staked SNS tokens are now permanently locked — no principal can disburse them, and the neuron cannot be dissolved or split.

The staked tokens remain in the governance canister's accounting but are unreachable. Unlike the Origin Protocol case (where funds could be recovered by re-adding the strategy), there is no recovery path once all permissions are removed from an SNS neuron.

**Impact class:** Governance authorization bug / permanent fund lockout.

### Likelihood Explanation

Granting `ManagePrincipals` to a canister is a standard pattern for SNS neuron automation (e.g., auto-voting, neuron management bots). The canister's controller can upgrade it at any time to include malicious logic. A neuron owner who trusts a third-party canister with `ManagePrincipals` is exposed to this attack if that canister is later compromised or turned adversarial. The attack requires no on-chain governance majority — a single `manage_neuron` ingress call from the malicious canister suffices.

### Recommendation

Before executing a `RemoveNeuronPermissions` command that would result in zero remaining permissions across all principals, the governance canister should:

1. Check whether `neuron.cached_neuron_stake_e8s > 0` or `neuron.maturity_e8s_equivalent > 0`.
2. If so, reject the operation with an error indicating that permissions cannot be fully removed while the neuron holds stake.

Alternatively, enforce that at least one principal always retains `ManagePrincipals` permission as long as the neuron has non-zero stake.

### Proof of Concept

```
1. Alice creates SNS neuron N with 1000 SNS tokens staked.
   cached_neuron_stake_e8s = 100_000_000_000

2. Alice grants Bob's canister C the ManagePrincipals permission on N.

3. Bob upgrades canister C with malicious code.

4. C sends ingress to SNS governance:
   manage_neuron({
     id: N,
     command: RemoveNeuronPermissions({
       principal_id: Alice,
       permissions_to_remove: [all permission types]
     })
   })
   → Succeeds. Alice can no longer manage N.

5. C sends ingress to SNS governance:
   manage_neuron({
     id: N,
     command: RemoveNeuronPermissions({
       principal_id: C,
       permissions_to_remove: [all permission types]
     })
   })
   → Succeeds. No principal has any permission on N.

6. Neuron N still exists in governance state with full stake,
   but no principal can call disburse, split, or dissolve.
   The 1000 SNS tokens are permanently locked.
```

The root cause — no stake check before allowing full permission removal — is located at: [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2301-2322)
```rust
    /// Removes a nervous system function from Governance if the given id for the nervous system
    /// function exists.
    fn perform_remove_generic_nervous_system_function(
        &mut self,
        id: u64,
    ) -> Result<(), GovernanceError> {
        let entry = self.proto.id_to_nervous_system_functions.entry(id);
        match entry {
            Entry::Vacant(_) => Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!(
                    "Failed to remove NervousSystemFunction. There is no NervousSystemFunction with id: {id}"
                ),
            )),
            Entry::Occupied(mut o) => {
                // Insert a deletion marker to signify that there was a NervousSystemFunction
                // with this id at some point, but that it was deleted.
                o.insert(NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER.clone());
                Ok(())
            }
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L4645-4658)
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

**File:** rs/sns/governance/src/proposal.rs (L1414-1429)
```rust
/// Validates and renders a proposal with action RemoveNervousSystemFunction.
pub fn validate_and_render_remove_nervous_generic_system_function(
    remove: u64,
    existing_functions: &BTreeMap<u64, NervousSystemFunction>,
) -> Result<String, String> {
    match existing_functions.get(&remove) {
        None => Err(format!("NervousSystemFunction: {remove} doesn't exist")),
        Some(function) => Ok(format!(
            r"# Proposal to remove existing NervousSystemFunction:

## Function:

{function:#?}"
        )),
    }
}
```
