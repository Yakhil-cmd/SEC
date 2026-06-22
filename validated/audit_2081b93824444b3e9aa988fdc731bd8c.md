### Title
`ManageVotingPermission` Revocation Does Not Clear Permissions Granted by the Revoked Principal - (`File: rs/sns/governance/src/governance.rs`)

### Summary

In SNS Governance, a principal holding `ManageVotingPermission` can grant that same permission (and other voting-related permissions) to additional principals. When the original principal's `ManageVotingPermission` is later revoked by the neuron owner, the permissions it granted to third parties persist. Those third parties retain the ability to vote, submit proposals, and further propagate `ManageVotingPermission` indefinitely, even though the authority chain that created them has been severed.

### Finding Description

`PERMISSIONS_RELATED_TO_VOTING` is defined in `rs/sns/governance/src/neuron.rs` as:

```rust
pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
    NeuronPermissionType::ManageVotingPermission,  // <-- includes itself
];
``` [1](#0-0) 

`check_principal_authorized_to_change_permissions` allows a caller holding only `ManageVotingPermission` to add any voting-related permission (including `ManageVotingPermission` itself) to any target principal:

```rust
let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
    vec![
        NeuronPermissionType::ManagePrincipals,
        NeuronPermissionType::ManageVotingPermission,
    ]
} else {
    vec![NeuronPermissionType::ManagePrincipals]
};
``` [2](#0-1) 

`is_exclusively_voting_related` confirms `ManageVotingPermission` passes the check: [3](#0-2) 

`remove_neuron_permissions` only removes permissions from the explicitly named principal. It performs no cascade — it does not clear permissions that the revoked principal previously granted to others: [4](#0-3) 

### Impact Explanation

A neuron owner who revokes a principal's `ManageVotingPermission` intends to remove that principal's voting influence over the neuron. However, if the principal had previously used `AddNeuronPermissions` to grant `ManageVotingPermission` (or `Vote`/`SubmitProposal`) to a second principal under their control, those permissions persist after revocation. The second principal can:

1. Continue to vote and submit proposals on behalf of the neuron, influencing SNS governance outcomes.
2. Further grant `ManageVotingPermission` to additional principals, creating an unbounded chain of persistent unauthorized voting influence.

This is a **governance authorization bug**: the neuron owner's revocation action does not achieve its intended effect of removing the revoked principal's influence.

### Likelihood Explanation

The attack requires a principal who was legitimately granted `ManageVotingPermission` to proactively grant it to a second principal they control before being revoked. This is a realistic scenario for a malicious or compromised delegate. The `AddNeuronPermissions` call is a standard ingress message requiring no special privileges beyond holding `ManageVotingPermission`. The neuron owner has no way to detect or prevent this pre-revocation grant without auditing the full permission history.

### Recommendation

When `remove_neuron_permissions` removes `ManageVotingPermission` (or `ManagePrincipals`) from a principal, the implementation should either:

1. **Cascade revocation**: Remove all voting-related permissions from principals that were granted by the revoked principal (requires tracking grant provenance, which is not currently stored).
2. **Prevent self-propagation**: Disallow a `ManageVotingPermission` holder from granting `ManageVotingPermission` to other principals (only allow granting `Vote` and `SubmitProposal`). Only `ManagePrincipals` holders should be able to grant `ManageVotingPermission`.

Option 2 is simpler and directly closes the escalation path. It requires changing `PERMISSIONS_RELATED_TO_VOTING` to exclude `ManageVotingPermission` from the set that a `ManageVotingPermission` holder can grant, or adding an explicit check in `add_neuron_permissions`.

### Proof of Concept

1. Neuron owner (Principal O) grants Principal A `ManageVotingPermission` via `AddNeuronPermissions`.
2. Principal A calls `manage_neuron` → `AddNeuronPermissions` targeting Principal B (a second identity controlled by A) with permissions `[Vote, SubmitProposal, ManageVotingPermission]`. This succeeds because `check_principal_authorized_to_change_permissions` accepts `ManageVotingPermission` as sufficient for voting-related grants. [5](#0-4) 
3. Principal O calls `manage_neuron` → `RemoveNeuronPermissions` to revoke Principal A's `ManageVotingPermission`. This succeeds and removes A's entry. [6](#0-5) 
4. Principal B still holds `[Vote, SubmitProposal, ManageVotingPermission]` in the neuron's `permissions` list. Principal B can now call `register_vote` and `make_proposal` on behalf of the neuron, and can grant these permissions to yet more principals. Principal O's revocation of A had no effect on B's permissions. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/neuron.rs (L61-65)
```rust
    pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
        NeuronPermissionType::Vote,
        NeuronPermissionType::SubmitProposal,
        NeuronPermissionType::ManageVotingPermission,
    ];
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

**File:** rs/sns/governance/src/neuron.rs (L152-159)
```rust
        let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
            vec![
                NeuronPermissionType::ManagePrincipals,
                NeuronPermissionType::ManageVotingPermission,
            ]
        } else {
            vec![NeuronPermissionType::ManagePrincipals]
        };
```

**File:** rs/sns/governance/src/governance.rs (L230-238)
```rust
    pub fn is_exclusively_voting_related(&self) -> bool {
        let permissions_related_to_voting = Neuron::PERMISSIONS_RELATED_TO_VOTING
            .iter()
            .map(|p| *p as i32)
            .collect::<Vec<_>>();
        self.permissions
            .iter()
            .all(|p| permissions_related_to_voting.contains(p))
    }
```

**File:** rs/sns/governance/src/governance.rs (L4596-4597)
```rust
        neuron
            .check_principal_authorized_to_change_permissions(caller, permissions_to_add.clone())?;
```

**File:** rs/sns/governance/src/governance.rs (L4699-4715)
```rust
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
