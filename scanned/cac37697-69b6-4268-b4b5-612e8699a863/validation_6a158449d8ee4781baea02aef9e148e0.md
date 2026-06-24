### Title
SNS Governance `RemoveNeuronPermissions` Lacks Last-Principal Guard, Enabling Permanent Neuron Lock — (`rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance `remove_neuron_permissions` function allows a caller with `ManagePrincipals` permission to remove all permissions from every principal on a neuron, permanently locking it with no recovery path. Unlike NNS neurons, which have a separate immutable `controller` field, SNS neurons rely entirely on the `permissions` list for all access control. The code itself explicitly acknowledges this danger but provides no guard. This is the direct IC analog to the H-01 "lack of last owner guard" vulnerability class.

---

### Finding Description

The SNS governance `manage_neuron` handler dispatches `RemoveNeuronPermissions` commands to `remove_neuron_permissions`:

```
rs/sns/governance/src/governance.rs, lines 4659–4716
```

The function's own docstring states:

> "This is a dangerous operation as it is possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token." [1](#0-0) 

Despite this acknowledgment, the function performs **no check** to ensure at least one principal retains `ManagePrincipals` (or any other permission) after the operation completes: [2](#0-1) 

The underlying `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs` uses `swap_remove` to physically remove the `NeuronPermission` entry when all permission types for a principal are cleared: [3](#0-2) 

When this is called for every principal on the neuron, `neuron.permissions` becomes an empty `Vec`. The integration test `test_neuron_remove_all_permissions_of_self` explicitly demonstrates and asserts this outcome: [4](#0-3) 

The proto definition for `RemoveNeuronPermissions` confirms the operation is exposed as a public `manage_neuron` command: [5](#0-4) 

**Contrast with NNS governance:** NNS neurons have a separate, immutable `controller` field and a `hot_keys` list. Removing all hot keys does not lock the neuron because the controller can still manage it via `configure`. SNS neurons have no such fallback — the `permissions` list is the sole access control mechanism. [6](#0-5) 

---

### Impact Explanation

When a neuron's `permissions` list is emptied:

- No principal can call `manage_neuron` on it (all commands require at least one permission).
- The neuron cannot be dissolved, its dissolve delay cannot be changed, and it cannot be disbursed.
- The staked SNS governance tokens are **permanently locked** in the neuron subaccount with no recovery path.
- Voting power is frozen and the neuron cannot participate in governance.

This is a direct loss-of-funds impact for the neuron owner.

---

### Likelihood Explanation

The `ManagePrincipals` permission is granted by default to the neuron claimer (`neuron_claimer_permissions`). Any neuron owner can trigger this path via a standard ingress `manage_neuron` call. Realistic scenarios include:

1. A user attempting to transfer full control to a new principal by removing their own permissions after granting them to another — if the grant step fails silently or is reordered, they lock themselves out.
2. A user "cleaning up" permissions without understanding that SNS neurons have no fallback controller.
3. A governance proposal or automated script that removes permissions as part of a migration, without verifying the post-state.

No privileged role, admin key, or subnet-majority corruption is required. The attacker-controlled entry path is a standard unprivileged ingress `manage_neuron` call.

---

### Recommendation

Add a last-principal guard inside `remove_neuron_permissions` (or `remove_permissions_for_principal`) that rejects any operation that would leave the neuron with zero principals holding `ManagePrincipals` permission. Concretely, after computing the post-removal permission state, verify:

```rust
let remaining_manage_principals = neuron.permissions.iter().any(|p| {
    p.permission_type.contains(&(NeuronPermissionType::ManagePrincipals as i32))
});
if !remaining_manage_principals {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Cannot remove the last ManagePrincipals permission from a neuron.",
    ));
}
```

This mirrors the mitigation applied to the Coinbase Smart Wallet: verify the post-state before committing the removal.

---

### Proof of Concept

1. Alice stakes SNS tokens and claims a neuron. She is granted all permissions including `ManagePrincipals`.
2. Alice calls `manage_neuron` with `RemoveNeuronPermissions { principal_id: alice, permissions_to_remove: NeuronPermissionList::all() }`.
3. `remove_neuron_permissions` passes all precondition checks (Alice has `ManagePrincipals`).
4. `remove_permissions_for_principal` removes all permission types; `remaining_permission_types` is empty, so `swap_remove` is called and Alice's `NeuronPermission` entry is deleted.
5. `neuron.permissions` is now empty (`len() == 0`), as confirmed by the existing integration test.
6. No principal can ever call `manage_neuron` on this neuron again. Alice's staked tokens are permanently locked. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L4659-4716)
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

**File:** rs/sns/integration_tests/src/neuron.rs (L2212-2265)
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

        Ok(())
    });
}
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

**File:** rs/nns/governance/src/neuron/types.rs (L678-690)
```rust
    /// Precondition: key to remove is present in 'hot_keys'
    fn remove_hot_key(&mut self, hot_key_to_remove: &PrincipalId) -> Result<(), GovernanceError> {
        if let Some(index) = self.hot_keys.iter().position(|x| *x == *hot_key_to_remove) {
            self.hot_keys.swap_remove(index);
            Ok(())
        } else {
            // Hot key to remove was not found.
            Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                "Remove failed: Hot key not found.",
            ))
        }
    }
```
