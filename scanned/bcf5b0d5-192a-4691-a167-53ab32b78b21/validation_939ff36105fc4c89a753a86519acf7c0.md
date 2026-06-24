### Title
Stale Neuron Permissions After `neuron_grantable_permissions` Reduction via `ManageNervousSystemParameters` - (File: rs/sns/governance/src/governance.rs)

### Summary
In SNS governance, when the `neuron_grantable_permissions` parameter is reduced via a `ManageNervousSystemParameters` proposal, existing neurons that already hold permissions no longer in the grantable set retain those permissions indefinitely. The `register_vote` (and other action) path checks only the permissions stored on the neuron, never the current `neuron_grantable_permissions`. There is no bulk-revocation mechanism, so the governance decision to restrict permissions has no effect on any neuron that was granted those permissions before the change.

### Finding Description

`NervousSystemParameters::neuron_grantable_permissions` is the SNS-wide ceiling on what permissions a principal with `ManagePrincipals` (or `ManageVotingPermission`) may grant to another principal for a neuron. The parameter is mutable via a `ManageNervousSystemParameters` governance proposal.

The proto comment explicitly documents the stale-state behavior:

> "If this set changes via a ManageNervousSystemParameters proposal, previous neurons' permissions will be unchanged and only newly granted permissions will be affected." [1](#0-0) 

When a neuron exercises a permission (e.g., `Vote`), the SNS governance `register_vote` function calls `neuron.check_authorized(caller, NeuronPermissionType::Vote)`: [2](#0-1) 

`check_authorized` / `is_authorized` look only at the `permissions` list stored on the neuron itself: [3](#0-2) 

`check_permissions_are_grantable` — the only place `neuron_grantable_permissions` is consulted — is called exclusively inside `add_neuron_permissions`, never inside `register_vote`, `make_proposal`, `configure_neuron`, or any other action path: [4](#0-3) [5](#0-4) 

Consequently, once a permission is stored on a neuron it remains exercisable regardless of any subsequent reduction of `neuron_grantable_permissions`.

### Impact Explanation

An SNS community that passes a `ManageNervousSystemParameters` proposal to remove `Vote` (or `SubmitProposal`, `ManagePrincipals`, `Disburse`, etc.) from `neuron_grantable_permissions` achieves no revocation of those permissions for any neuron that already holds them. Principals that were granted `Vote` before the change can continue to cast votes and influence governance outcomes; principals with `SubmitProposal` can continue to submit proposals and burn stake; principals with `ManagePrincipals` can continue to grant/revoke permissions on neurons they do not own. The SNS community has no single-transaction bulk-revocation path — they would need to issue individual `RemoveNeuronPermissions` calls for every affected neuron, which may be infeasible at scale (up to `max_number_of_neurons` = 200,000 neurons).

### Likelihood Explanation

The trigger is a legitimate governance action (a `ManageNervousSystemParameters` proposal) that any SNS community may pass. The stale-permission window begins immediately after the proposal executes and persists until each affected neuron is individually remediated. Any principal that was previously granted a now-restricted permission and is aware of the change can exploit the window by simply calling `manage_neuron` before remediation. The entry path requires no privileged access — only a valid ingress call from the principal that holds the stale permission.

### Recommendation

1. **Enforce `neuron_grantable_permissions` at exercise time**, not only at grant time. In `register_vote`, `make_proposal`, and other action handlers, add a check that the permission being exercised is still present in the current `neuron_grantable_permissions`, or document clearly that the parameter is append-only and cannot be reduced.
2. **Alternatively**, if reduction is intended to be a valid operation, provide a governance proposal type (or a batch variant of `RemoveNeuronPermissions`) that atomically revokes a specified permission from all neurons in the SNS, making the governance intent enforceable.
3. At minimum, surface a clear warning in the `ManageNervousSystemParameters` execution path when a permission is being removed from `neuron_grantable_permissions`, so SNS communities understand that existing grants are unaffected.

### Proof of Concept

```
1. Deploy SNS with neuron_grantable_permissions = [Vote, SubmitProposal, ManagePrincipals, ...].

2. Neuron owner (Alice) calls manage_neuron → AddNeuronPermissions to grant
   Vote to attacker principal (Bob) on Neuron X.
   → Succeeds: check_permissions_are_grantable passes because Vote ∈ neuron_grantable_permissions.
   → Bob's Vote permission is now stored in Neuron X's permissions list.

3. SNS community passes ManageNervousSystemParameters proposal:
     neuron_grantable_permissions = [SubmitProposal, ManagePrincipals, ...]  // Vote removed

4. Bob calls manage_neuron → RegisterVote on Neuron X for any open proposal.
   → governance.rs:3870: neuron.check_authorized(bob, NeuronPermissionType::Vote)
   → neuron.rs:130-136: finds Bob's entry in neuron.permissions; Vote is present → Ok(())
   → neuron_grantable_permissions is never consulted.
   → Vote is cast successfully, contrary to the SNS community's governance intent.

5. No bulk revocation exists. Alice must manually call RemoveNeuronPermissions
   for every neuron that was granted Vote — potentially tens of thousands.
``` [6](#0-5) [7](#0-6) [1](#0-0)

### Citations

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1218-1223)
```text
  // The superset of neuron permissions a principal with permission
  // `NeuronPermissionType::ManagePrincipals` for a given neuron can grant to another
  // principal for this same neuron.
  // If this set changes via a ManageNervousSystemParameters proposal, previous
  // neurons' permissions will be unchanged and only newly granted permissions will be affected.
  optional NeuronPermissionList neuron_grantable_permissions = 16;
```

**File:** rs/sns/governance/src/governance.rs (L3854-3870)
```rust
    fn register_vote(
        &mut self,
        neuron_id: &NeuronId,
        caller: &PrincipalId,
        request: &manage_neuron::RegisterVote,
    ) -> Result<(), GovernanceError> {
        let now_seconds = self.env.now();

        let neuron = self
            .proto
            .neurons
            .get_mut(&neuron_id.to_string())
            .ok_or_else(||
                // The specified neuron is not present.
                GovernanceError::new_with_message(ErrorType::NotFound, "Neuron not found"))?;

        neuron.check_authorized(caller, NeuronPermissionType::Vote)?;
```

**File:** rs/sns/governance/src/governance.rs (L4596-4601)
```rust
        neuron
            .check_principal_authorized_to_change_permissions(caller, permissions_to_add.clone())?;

        self.nervous_system_parameters_or_panic()
            .check_permissions_are_grantable(permissions_to_add)?;

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

**File:** rs/sns/governance/src/types.rs (L940-973)
```rust
    /// Given a NeuronPermissionList, check whether the provided list can be
    /// granted given the `NervousSystemParameters::neuron_grantable_permissions`.
    /// Format a useful error if not.
    pub fn check_permissions_are_grantable(
        &self,
        neuron_permission_list: &NeuronPermissionList,
    ) -> Result<(), GovernanceError> {
        let mut illegal_permissions = HashSet::new();

        let grantable_permissions: HashSet<&i32> = self
            .neuron_grantable_permissions
            .as_ref()
            .expect("NervousSystemParameters.neuron_grantable_permissions must be present")
            .permissions
            .iter()
            .collect();

        for permission in &neuron_permission_list.permissions {
            if !grantable_permissions.contains(&permission) {
                illegal_permissions.insert(NeuronPermissionType::try_from(*permission).ok());
            }
        }

        if !illegal_permissions.is_empty() {
            return Err(GovernanceError::new_with_message(
                ErrorType::AccessControlList,
                format!(
                    "Cannot grant permissions as one or more permissions is not \
                    allowed to be granted. Illegal Permissions: {illegal_permissions:?}"
                ),
            ));
        }

        Ok(())
```
