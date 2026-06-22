### Title
SNS Governance `remove_neuron_permissions` Allows Removing All Permissions Without Validating Neuron Remains Accessible - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `remove_neuron_permissions` function permits a caller to remove all permissions from a neuron — including the last `ManagePrincipals` holder — without validating that the neuron retains at least one principal capable of managing or disbursing it. This is a direct analog to the FT4 `update_main_auth_descriptor` bug: both allow a privileged operation to leave a controlled asset in a permanently inaccessible state.

---

### Finding Description

`remove_neuron_permissions` in `rs/sns/governance/src/governance.rs` performs the following checks before executing:

1. The caller holds `ManagePrincipals` (or `ManageVotingPermission` for voting-only removals).
2. The target principal actually holds the permissions being removed.

It does **not** check whether, after the removal, the neuron still has at least one principal holding `ManagePrincipals` (or `Disburse`). The code itself acknowledges this danger in its docstring:

> "This is a dangerous operation as it is possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token." [1](#0-0) 

The function proceeds to call `remove_permissions_for_principal`, which will happily remove the last `ManagePrincipals` entry and return `AllPermissionTypesRemoved` with no post-condition check: [2](#0-1) 

The underlying `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs` removes the `NeuronPermission` entry entirely when all permission types are gone, with no invariant enforcement: [3](#0-2) 

This is confirmed by the integration test `test_neuron_remove_all_permissions_of_self`, which explicitly verifies that a user **can** remove all their own permissions, leaving `neuron.permissions.len() == 0`: [4](#0-3) 

By contrast, `REQUIRED_NEURON_CLAIMER_PERMISSIONS` enforces that `ManagePrincipals`, `Vote`, and `SubmitProposal` are present **at neuron creation time**, but no equivalent invariant is enforced on removal: [5](#0-4) 

The `validate_neuron_grantable_permissions` function only checks that the field is set — it does not validate that the resulting permission set after removal is non-empty or retains required permissions: [6](#0-5) 

---

### Impact Explanation

When a neuron's last `ManagePrincipals` holder removes that permission from themselves (or a co-principal with `ManagePrincipals` removes it from the last holder), the neuron enters a permanently inaccessible state:

- No principal can call `add_neuron_permissions` (requires `ManagePrincipals`).
- No principal can call `remove_neuron_permissions` (requires `ManagePrincipals`).
- If `Disburse` was also removed, no principal can recover the staked SNS governance tokens.
- The neuron's staked tokens are permanently locked in the SNS ledger subaccount with no recovery path.

This is a **governance authorization bug** resulting in **permanent ledger conservation loss** for the affected neuron's staked tokens.

---

### Likelihood Explanation

The most realistic scenarios:

1. **Accidental self-lock**: A neuron owner with a single-principal permission set removes `ManagePrincipals` from themselves (e.g., intending to remove it from a different principal, or misunderstanding the operation). The neuron is then permanently locked.
2. **Griefing by co-principal**: If two principals share `ManagePrincipals`, one can remove `ManagePrincipals` from the other, then remove it from themselves, locking the neuron.
3. **Programmatic error**: An SNS dapp or wallet that automates permission management could trigger this accidentally.

The entry path is a standard ingress `manage_neuron` call — no privileged access beyond owning the neuron is required. The operation is explicitly exposed in the public Candid interface. [7](#0-6) 

---

### Recommendation

After executing `remove_permissions_for_principal`, validate that the neuron still has at least one principal holding `ManagePrincipals`. If the removal would leave the neuron with no `ManagePrincipals` holder, reject the operation with an appropriate error. This mirrors the fix recommended for the FT4 `update_main_auth_descriptor` bug: validate the post-state has the required access flags before committing the change.

Specifically, in `remove_neuron_permissions` (`rs/sns/governance/src/governance.rs`), after line 4705, check that the neuron's updated permissions still contain at least one principal with `ManagePrincipals`. If not, return a `PreconditionFailed` error.

---

### Proof of Concept

```
1. Create an SNS neuron with neuron_claimer_permissions = [ManagePrincipals, Vote, SubmitProposal, Disburse, ...]
2. Call manage_neuron with:
   Command::RemoveNeuronPermissions(RemoveNeuronPermissions {
       principal_id: Some(owner_principal),
       permissions_to_remove: Some(NeuronPermissionList {
           permissions: NeuronPermissionType::all(),  // all permissions
       }),
   })
3. The call succeeds (confirmed by test_neuron_remove_all_permissions_of_self).
4. neuron.permissions.len() == 0.
5. No principal can now disburse, split, vote, or manage the neuron.
6. Staked SNS tokens in the neuron's ledger subaccount are permanently inaccessible.
``` [8](#0-7)

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

**File:** rs/sns/governance/src/neuron.rs (L782-792)
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

**File:** rs/sns/governance/src/types.rs (L437-445)
```rust
    pub const REQUIRED_NEURON_CLAIMER_PERMISSIONS: &'static [NeuronPermissionType] = &[
        // Without this permission, it would be impossible to transfer control
        // of a neuron to a new principal.
        NeuronPermissionType::ManagePrincipals,
        // Without this permission, it would be impossible to vote.
        NeuronPermissionType::Vote,
        // Without this permission, it would be impossible to submit a proposal.
        NeuronPermissionType::SubmitProposal,
    ];
```

**File:** rs/sns/governance/src/types.rs (L869-876)
```rust
    /// Validates that the nervous system parameter neuron_grantable_permissions is well-formed.
    fn validate_neuron_grantable_permissions(&self) -> Result<(), String> {
        self.neuron_grantable_permissions.as_ref().ok_or_else(|| {
            "NervousSystemParameters.neuron_grantable_permissions must be set".to_string()
        })?;

        Ok(())
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
