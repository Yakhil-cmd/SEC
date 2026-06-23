### Title
SNS Governance `remove_neuron_permissions` Allows Permanently Locking Staked Tokens by Removing All Permissions Without Checking Neuron Stake - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister's `remove_neuron_permissions` function permits removing all `NeuronPermission` entries from a neuron that still holds a non-zero `cached_neuron_stake_e8s`. Once all permissions are stripped, no principal can call `disburse_neuron` (which requires `NeuronPermissionType::Disburse`), permanently locking the staked SNS tokens in the neuron's ledger subaccount with no recovery path.

### Finding Description

`remove_neuron_permissions` in `rs/sns/governance/src/governance.rs` performs no check on the neuron's stake before allowing all permissions to be removed: [1](#0-0) 

The function's own documentation acknowledges the danger:

> "If all the permissions are removed from the Neuron i.e. by removing all permissions for all PrincipalIds, the Neuron is not deleted. This is a dangerous operation as it is possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token."

The underlying `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs` simply removes the `NeuronPermission` entry when all permission types are exhausted, with no stake guard: [2](#0-1) 

`disburse_neuron` in `rs/sns/governance/src/governance.rs` requires `NeuronPermissionType::Disburse` to be present on the caller: [3](#0-2) 

Once all permissions are removed, this check will always fail for every principal, making the staked tokens permanently inaccessible.

The `RemoveNeuronPermissions` command is exposed as a standard `manage_neuron` ingress endpoint: [4](#0-3) 

### Impact Explanation

Staked SNS tokens held in the neuron's governance ledger subaccount become permanently locked. The neuron record persists in governance state but no principal can disburse, split, or otherwise recover the tokens. This is an irreversible ledger conservation violation: tokens are minted and held on-chain but rendered permanently inaccessible. The neuron also continues to consume governance state storage indefinitely.

### Likelihood Explanation

Any principal granted `ManagePrincipals` permission on a neuron — which is included in the default `neuron_claimer_permissions` granted to every neuron creator — can trigger this condition. A malicious co-controller granted `ManagePrincipals` can remove all permissions from all principals (including themselves) on a neuron with non-zero stake, permanently destroying the owner's funds. The attack requires only a standard ingress `manage_neuron` call; no privileged access, governance majority, or subnet compromise is needed. [5](#0-4) 

### Recommendation

**Short term**: Add a precondition check in `remove_neuron_permissions` that rejects the operation when it would result in a neuron with zero total permissions and a non-zero `cached_neuron_stake_e8s` (or non-zero `maturity_e8s_equivalent`). Specifically, after computing the would-be resulting permission set, verify that at least one principal retains `NeuronPermissionType::Disburse` if the neuron's stake is non-zero.

**Long term**: Document and enforce the SNS neuron state machine invariant that a neuron with non-zero stake must always have at least one principal holding `Disburse` permission. Add invariant-based fuzz tests that verify this property is preserved across all `manage_neuron` commands.

### Proof of Concept

1. User A stakes SNS tokens and claims a neuron, receiving all default permissions including `ManagePrincipals` and `Disburse`.
2. User A grants User B (attacker) `ManagePrincipals` permission via `AddNeuronPermissions`.
3. User B calls `manage_neuron` with `RemoveNeuronPermissions` targeting User A, removing all of User A's permissions (including `Disburse`).
4. User B then calls `manage_neuron` with `RemoveNeuronPermissions` targeting themselves, removing all of their own permissions.
5. The neuron now has `permissions: []` but `cached_neuron_stake_e8s > 0`.
6. Any subsequent call to `disburse_neuron` by any principal fails with `NotAuthorized` because `check_authorized` finds no matching `Disburse` permission entry.
7. The staked SNS tokens are permanently locked.

This is confirmed by the integration test `test_neuron_remove_all_permissions_of_self` which demonstrates that removing all permissions from a staked neuron succeeds without error: [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1125-1127)
```rust
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

**File:** rs/sns/integration_tests/src/neuron.rs (L2212-2264)
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
```
