### Title
SNS Neuron `ManagePrincipals` Permission Can Be Permanently Lost, Freezing Staked Tokens - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `remove_neuron_permissions` function contains no guard preventing the removal of the last `ManagePrincipals` permission from a neuron. A neuron owner can permanently lock their neuron — and all staked tokens within it — by first granting `ManagePrincipals` to an unowned/burn address and then revoking their own `ManagePrincipals`. This is the direct IC analog of the Naym Diamond `sysAdminCount` manipulation: the "only admin grants to an unowned address, then revokes themselves" scenario.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `remove_neuron_permissions` function processes `RemoveNeuronPermissions` commands. It checks only that:
1. The caller holds `ManagePrincipals` (or `ManageVotingPermission` for voting-related permissions).
2. The target principal actually holds the permissions being removed.

There is **no check** that at least one principal will retain `ManagePrincipals` after the operation completes. [1](#0-0) 

The function's own docstring acknowledges the danger: [2](#0-1) 

The underlying `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs` similarly performs no such invariant check — it simply removes the requested permissions and, if all are gone, removes the principal entry entirely: [3](#0-2) 

The `add_neuron_permissions` function does allow granting `ManagePrincipals` to any arbitrary `PrincipalId`, including addresses that no one controls: [4](#0-3) 

The `ManagePrincipals` permission is the root access-control permission for a neuron — it is the only permission that allows adding or removing non-voting permissions: [5](#0-4) 

---

### Impact Explanation

When a neuron loses all `ManagePrincipals` holders:

- No principal can ever call `AddNeuronPermissions` or `RemoveNeuronPermissions` on the neuron again (the authorization check at line 4694 will always fail).
- The neuron's `Disburse`, `Split`, `ConfigureDissolveState`, and all other permissions become permanently unmodifiable.
- If the sole remaining principal (the unowned address) also holds `Disburse`, the staked SNS tokens are permanently frozen — they can never be recovered.
- The neuron continues to exist in governance state, consuming resources and potentially holding voting power, but is completely uncontrollable.

This is a **permanent, irreversible loss of staked governance tokens** for the neuron owner.

---

### Likelihood Explanation

The likelihood is **low-to-medium**:

- It requires the neuron owner to hold `ManagePrincipals` (which is granted by default via `neuron_claimer_permissions`). [6](#0-5) 

- The two-step sequence (grant to unowned address → revoke self) can occur accidentally (e.g., a typo in the target `PrincipalId` followed by a self-revocation) or through a confused/malicious UI.
- The operation is reachable by any unprivileged ingress sender who owns a neuron — no special privilege beyond neuron ownership is required.
- The integration test `test_neuron_remove_all_permissions_of_self` confirms this path is reachable and succeeds without error: [7](#0-6) 

---

### Recommendation

1. **Add a guard in `remove_neuron_permissions`**: Before executing the removal, verify that after the operation at least one principal in `neuron.permissions` will still hold `ManagePrincipals`. If not, return an error.

2. **Implement a two-step transfer for `ManagePrincipals`**: Require that a new `ManagePrincipals` holder explicitly accepts the role (analogous to a two-step ownership transfer) before the original holder can revoke themselves. This prevents accidental grants to unowned addresses.

3. **At minimum**, add a check in `add_neuron_permissions` that prevents granting `ManagePrincipals` to a principal that already holds it (to prevent any future counter-inflation if the model changes), and document the invariant that at least one `ManagePrincipals` holder must always exist.

---

### Proof of Concept

**Entry path** (unprivileged ingress sender, standard neuron owner):

```
// Step 1: Alice (sole ManagePrincipals holder) grants ManagePrincipals to a burn address
manage_neuron({
  subaccount: alice_subaccount,
  command: AddNeuronPermissions({
    principal_id: Some(BURN_ADDRESS),   // unowned, no one controls this
    permissions_to_add: [ManagePrincipals, Disburse, ...]
  })
})
// Succeeds: add_neuron_permissions has no check against unowned addresses
// rs/sns/governance/src/governance.rs:4619-4634

// Step 2: Alice revokes her own ManagePrincipals
manage_neuron({
  subaccount: alice_subaccount,
  command: RemoveNeuronPermissions({
    principal_id: Some(ALICE),
    permissions_to_remove: [ManagePrincipals]
  })
})
// Succeeds: remove_neuron_permissions has no last-ManagePrincipals guard
// rs/sns/governance/src/governance.rs:4694-4715

// Result: neuron.permissions = [{principal: BURN_ADDRESS, permissions: [ManagePrincipals, ...]}]
// Alice's staked tokens are permanently frozen.
// No ingress call can ever recover them.
```

The `remove_permissions_for_principal` function will succeed and return `SomePermissionTypesRemoved` (Alice still has other permissions) or `AllPermissionTypesRemoved` (if Alice had only `ManagePrincipals`), with no invariant check in either branch: [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4334-4337)
```rust
            permissions: vec![NeuronPermission::new(
                principal_id,
                self.neuron_claimer_permissions_or_panic().permissions,
            )],
```

**File:** rs/sns/governance/src/governance.rs (L4619-4634)
```rust
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

**File:** rs/sns/governance/src/neuron.rs (L782-793)
```rust
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

**File:** rs/sns/integration_tests/src/neuron.rs (L2212-2261)
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
```
