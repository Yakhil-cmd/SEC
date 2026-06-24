### Title
Single Principal with `ManagePrincipals` Can Permanently Lock All Staked Tokens in an SNS Neuron by Removing All Permissions — (`rs/sns/governance/src/governance.rs`)

---

### Summary

In the SNS Governance canister, any principal holding the `ManagePrincipals` permission on a neuron can call `manage_neuron` → `RemoveNeuronPermissions` to strip every permission from every principal on that neuron — including themselves — in one or more sequential ingress calls. Once the neuron's permission list is empty, no principal can ever call `Disburse`, `DisburseMaturity`, `ConfigureDissolveState`, or any other neuron operation again. The staked governance tokens are permanently locked with no on-chain remediation path.

---

### Finding Description

`remove_neuron_permissions` in `rs/sns/governance/src/governance.rs` enforces only one precondition before executing: the caller must hold `ManagePrincipals` (or `ManageVotingPermission` for voting-only changes). [1](#0-0) 

The authorization helper `check_principal_authorized_to_change_permissions` checks only that the caller has the right permission type; it places no restriction on *which* principal's permissions are being removed, nor does it prevent the caller from removing their own `ManagePrincipals` permission. [2](#0-1) 

The underlying mutation `remove_permissions_for_principal` explicitly allows the result to be an empty permission list: [3](#0-2) 

The code and proto both acknowledge this is dangerous but provide no guard: [4](#0-3) [5](#0-4) 

An integration test confirms the behavior is reachable and succeeds end-to-end: [6](#0-5) 

**Attack sequence for a multi-principal neuron (direct analog to the external report):**

1. Neuron N has principals A (owner) and B (co-manager), both holding `ManagePrincipals`.
2. A (malicious/compromised) calls `manage_neuron` → `RemoveNeuronPermissions` targeting B with all of B's permissions. This succeeds because A has `ManagePrincipals`.
3. A then calls `manage_neuron` → `RemoveNeuronPermissions` targeting themselves, removing their own `ManagePrincipals` and `Disburse` permissions.
4. The neuron now has `permissions.len() == 0`. No principal can call `Disburse`, `DisburseMaturity`, `ConfigureDissolveState`, `Split`, or `AddNeuronPermissions` ever again.
5. The staked tokens remain locked in the governance canister's ledger subaccount indefinitely.

`ManagePrincipals` is granted by default to every neuron claimer via `REQUIRED_NEURON_CLAIMER_PERMISSIONS`: [7](#0-6) 

The `Disburse` operation requires the `NeuronPermissionType::Disburse` permission: [8](#0-7) 

Once all permissions are removed, this check will always fail for every caller, permanently blocking fund recovery.

---

### Impact Explanation

Staked governance tokens held in the neuron's ledger subaccount become permanently inaccessible. No principal can disburse, split, or otherwise recover the tokens. The neuron cannot be dissolved or reconfigured. This is a permanent, irreversible DoS on the neuron's funds — directly analogous to the "holding users' funds hostage" impact in the external report.

---

### Likelihood Explanation

The `ManagePrincipals` permission is granted by default to every neuron claimer. Any SNS deployment that allows multiple principals to share a neuron (e.g., a team-controlled neuron, a DAO treasury neuron, or any neuron where `AddNeuronPermissions` has been used) is exposed. The attack requires only standard ingress calls to the SNS Governance canister — no special keys, no governance majority, no subnet compromise. A single compromised or malicious co-manager is sufficient.

---

### Recommendation

Add a guard in `remove_neuron_permissions` that prevents the operation from leaving a neuron with zero principals holding `ManagePrincipals` (or zero principals total). Concretely:

- After computing the post-removal permission state, check whether at least one principal would still hold `ManagePrincipals`. If not, reject the call with an appropriate error.
- Alternatively, prevent a caller from removing their own last `ManagePrincipals` permission if they are the sole holder of that permission on the neuron.

This mirrors the recommendation in the external report: add a higher-level invariant that the access-control list can never be left in an irrecoverable state.

---

### Proof of Concept

The existing integration test `test_neuron_remove_all_permissions_of_self` already demonstrates the full attack path end-to-end: [9](#0-8) 

After the call, `neuron.permissions.len() == 0` is asserted, confirming that no principal retains any permission. Extending this test to then attempt a `Disburse` call would confirm the permanent fund lock.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1125-1127)
```rust
        // First check authorized
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L2022-2025)
```text
  // Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
  // its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
  // operation as it's possible to remove all permissions for a neuron and no longer be able to modify
  // its state, i.e. disbursing the neuron back into the governance token.
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
