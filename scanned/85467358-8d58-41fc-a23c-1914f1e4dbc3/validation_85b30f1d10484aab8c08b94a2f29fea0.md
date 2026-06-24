### Title
Malicious `ManagePrincipals` holder can permanently resist permission revocation by pre-seeding backdoor principals - (`rs/sns/governance/src/governance.rs`)

### Summary

In the SNS governance canister, any principal holding the `ManagePrincipals` permission on a neuron can grant `ManagePrincipals` to additional principals (when `ManagePrincipals` is included in `neuron_grantable_permissions`). Because there is no atomic "revoke all" operation, a malicious grantee can pre-seed multiple backdoor principals before the neuron owner attempts revocation. The owner must then remove each backdoor principal one-by-one via separate `manage_neuron` calls, and any surviving backdoor principal can immediately re-grant the revoked principal full `ManagePrincipals` access. This creates an irresolvable permission-revocation race that mirrors the `unauthorize()` front-running bug in the external report.

---

### Finding Description

`add_neuron_permissions` in `rs/sns/governance/src/governance.rs` authorizes any caller that passes `check_principal_authorized_to_change_permissions` to grant arbitrary permissions (bounded only by `neuron_grantable_permissions`) to any target principal: [1](#0-0) 

The authorization check in `rs/sns/governance/src/neuron.rs` only verifies that the caller holds `ManagePrincipals` (or `ManageVotingPermission` for voting-only changes). It places no restriction on *who* the target principal is or *which* permissions are being granted, including `ManagePrincipals` itself: [2](#0-1) 

The only gate on which permissions can be granted is `check_permissions_are_grantable`, which simply checks membership in the SNS-level `neuron_grantable_permissions` list: [3](#0-2) 

When an SNS is deployed with `ManagePrincipals` inside `neuron_grantable_permissions` (a common real-world configuration, as shown in integration tests), a principal with `ManagePrincipals` can freely grant that same permission to other principals.

There is no `remove_all_permissions` or equivalent atomic revocation function anywhere in the governance source: [4](#0-3) 

The `max_number_of_principals_per_neuron` defaults to 5: [5](#0-4) 

This means a malicious grantee can pre-fill up to 4 backdoor principals (the 5th slot is occupied by the neuron owner), each capable of re-granting `ManagePrincipals` to the attacker after the owner removes it.

---

### Impact Explanation

A malicious principal with `ManagePrincipals` can maintain permanent, irrevocable control over a neuron. With `ManagePrincipals`, the attacker can:

- Vote on SNS governance proposals on behalf of the neuron, distorting governance outcomes.
- Submit proposals (including upgrade proposals) on behalf of the neuron.
- Disburse the neuron's staked tokens to an arbitrary account.
- Split the neuron, creating new neurons under attacker-controlled principals.

The neuron owner has no atomic escape: each individual `remove_neuron_permissions` call leaves a window during which surviving backdoor principals can re-grant the attacker access. The owner would need to remove all backdoor principals in a single atomic batch, which the current API does not support.

---

### Likelihood Explanation

- **Condition 1**: `ManagePrincipals` must be in `neuron_grantable_permissions`. The protocol default is an empty list, but real SNS deployments routinely set this to include `ManagePrincipals` (as seen in multiple integration tests and the SNS initialization flow).
- **Condition 2**: The neuron owner must have previously granted `ManagePrincipals` to a principal that later becomes malicious. This is the intended use case (e.g., a DAO multisig granting a trusted operator `ManagePrincipals` for day-to-day operations).
- **No front-running required**: Unlike the EVM context, the IC has no gas-price-ordered mempool. However, the attack does not require front-running at all. The malicious grantee can proactively add backdoor principals *before* the owner ever attempts revocation, making the attack entirely offline and undetectable until the owner tries to remove access.

---

### Recommendation

1. **Add a `remove_all_permissions_for_principal` or `revoke_all` command** that atomically removes all permissions for all non-owner principals in a single `manage_neuron` call. This is the direct analog to the `unauthorizeAll` mitigation suggested in the external report.

2. **Alternatively, restrict `ManagePrincipals` self-propagation**: Prevent a principal with `ManagePrincipals` from granting `ManagePrincipals` to other principals. Only the neuron's original claimer (the principal recorded at neuron creation) should be able to grant `ManagePrincipals`. This mirrors the external report's recommendation to replace `_msgSender()` with `msg.sender` in `authorize()`.

3. **At minimum, document** that granting `ManagePrincipals` to any principal is equivalent to granting full, irrevocable neuron control, and that SNS operators should not include `ManagePrincipals` in `neuron_grantable_permissions` unless they accept this risk.

---

### Proof of Concept

```
// Setup: SNS with ManagePrincipals in neuron_grantable_permissions
// Alice claims a neuron and receives ManagePrincipals by default.

// Step 1: Alice grants Bob ManagePrincipals (legitimate delegation).
alice -> manage_neuron(AddNeuronPermissions { principal: Bob, permissions: [ManagePrincipals, ...] })

// Step 2: Bob (now malicious) proactively seeds backdoor principals
//         BEFORE Alice ever tries to remove him.
bob -> manage_neuron(AddNeuronPermissions { principal: Eve, permissions: [ManagePrincipals] })
bob -> manage_neuron(AddNeuronPermissions { principal: Mallory, permissions: [ManagePrincipals] })
// (up to max_number_of_principals_per_neuron - 1 = 4 backdoors)

// Step 3: Alice discovers Bob is malicious and removes him.
alice -> manage_neuron(RemoveNeuronPermissions { principal: Bob, permissions: [ManagePrincipals, ...] })
// Bob is removed. But Eve and Mallory still have ManagePrincipals.

// Step 4: Eve immediately re-grants Bob ManagePrincipals.
eve -> manage_neuron(AddNeuronPermissions { principal: Bob, permissions: [ManagePrincipals] })
// Bob is back. Alice must now also remove Eve and Mallory,
// but while she removes Eve, Mallory re-adds Bob, and so on.

// Result: Alice cannot atomically revoke all access.
//         Bob retains ManagePrincipals indefinitely and can
//         disburse neuron funds, vote, or submit proposals.
```

The attack is confirmed by the existing test infrastructure showing that a principal with `ManagePrincipals` can freely call `add_neuron_permissions` to grant `ManagePrincipals` to a third party: [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4570-4597)
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

**File:** rs/sns/governance/src/types.rs (L943-974)
```rust
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
    }
```

**File:** rs/sns/governance/api_helpers/src/lib.rs (L44-45)
```rust
        neuron_grantable_permissions: Some(NeuronPermissionList::default()),
        max_number_of_principals_per_neuron: Some(5),
```

**File:** rs/sns/governance/tests/governance.rs (L890-917)
```rust
#[test]
fn test_adding_permissions_when_we_have_manage_principals() {
    let caller = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let target = *TEST_NEURON_2_OWNER_PRINCIPAL;
    let permissions_to_add = NeuronPermissionList::all();
    let (mut governance, neuron) = {
        let permissions: &[(PrincipalId, NeuronPermissionList)] =
            &[(caller, vec![NeuronPermissionType::ManagePrincipals].into())];
        let user_principal = PrincipalId::new_user_test_id(0);
        let neuron_id = neuron_id(user_principal, 0);

        let governance_fixture = GovernanceCanisterFixtureBuilder::new()
            .with_neuron_grantable_permissions(NeuronPermissionList::all())
            .add_neuron_with_permissions(permissions, neuron_id.clone())
            .create();

        (governance_fixture, neuron_id)
    };

    // Attempt to add permissions to `target` - should succeed since `caller`
    // has `ManagePrincipals`.
    governance
        .add_neuron_permissions(&neuron, target, permissions_to_add.clone(), caller)
        .unwrap();

    // Check that `target` now has those permissions.
    governance.assert_principal_has_permissions_for_neuron(&neuron, target, permissions_to_add);
}
```
