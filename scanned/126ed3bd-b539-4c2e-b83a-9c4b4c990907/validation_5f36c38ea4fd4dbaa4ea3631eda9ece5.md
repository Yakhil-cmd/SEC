### Title
SNS Neuron Permanently Inaccessible After All Permissions Removed — No Recovery Path (`rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister allows any principal holding `ManagePrincipals` permission on a neuron to remove **all** permissions from **all** principals on that neuron. Once this happens, the neuron's staked tokens are permanently locked: no principal can disburse, dissolve, or otherwise manage the neuron, and no recovery function exists. This is a direct analog to the "forbidden manager can never use pool" class of irreversible access-control lock.

---

### Finding Description

`remove_neuron_permissions` in `rs/sns/governance/src/governance.rs` allows a caller with `ManagePrincipals` to remove any set of permissions from any principal on a neuron, including removing all permissions from themselves: [1](#0-0) 

The underlying `remove_permissions_for_principal` in `rs/sns/governance/src/neuron.rs` explicitly permits the resulting empty-permissions state: [2](#0-1) 

The neuron is **not deleted** when all permissions are removed. The proto definition and its Rust generated code both document this as a known dangerous operation with no safeguard: [3](#0-2) [4](#0-3) 

The integration test `test_neuron_remove_all_permissions_of_self` confirms the end state is reachable and results in `neuron.permissions.len() == 0`: [5](#0-4) 

There is no privileged recovery path. `add_neuron_permissions` requires the caller to already hold `ManagePrincipals` on the target neuron: [6](#0-5) 

Once the permissions list is empty, no principal satisfies this check, making the lock permanent.

---

### Impact Explanation

Staked SNS governance tokens held in the neuron become permanently inaccessible. The neuron cannot be dissolved, disbursed, voted with, or modified in any way. The locked tokens are effectively removed from circulation without being burned through any legitimate mechanism. This constitutes a **governance authorization bug** with a secondary **ledger conservation** impact: token supply accounting diverges from user-accessible balances.

---

### Likelihood Explanation

The trigger is reachable by any unprivileged ingress sender who owns (or has `ManagePrincipals` on) an SNS neuron. Two realistic paths exist:

1. **Accidental self-lock**: A neuron owner removes their own last permission while attempting to clean up their ACL, leaving the neuron permanently inaccessible.
2. **Griefing**: A principal with `ManagePrincipals` on a shared neuron (e.g., a co-controller) removes all permissions from all parties, permanently locking the other parties' staked tokens.

No privileged access, key compromise, or majority attack is required.

---

### Recommendation

Add a guard in `remove_neuron_permissions` that prevents the operation from leaving a neuron with zero total permissions across all principals. Specifically, before committing the removal, verify that at least one principal retains `ManagePrincipals` after the change. If the operation would result in a fully permission-less neuron, return a `GovernanceError` with type `PreconditionFailed`.

Alternatively, introduce a privileged SNS root/governance-level recovery proposal action that can forcibly re-grant permissions to a specified principal on a neuron that has an empty permissions list.

---

### Proof of Concept

1. Stake tokens and claim an SNS neuron, receiving `ManagePrincipals` and all other permissions.
2. Call `manage_neuron` with `Command::RemoveNeuronPermissions`, specifying your own `principal_id` and `NeuronPermissionType::all()` as `permissions_to_remove`.
3. Observe `neuron.permissions.len() == 0` — confirmed by the existing integration test at: [7](#0-6) 

4. Attempt any subsequent `manage_neuron` call (disburse, dissolve, add permissions) — all fail with `NotAuthorized` because no principal holds any permission on the neuron.
5. No SNS proposal type exists to recover the neuron's permission list, confirming the permanent lock.

### Citations

**File:** rs/sns/governance/src/governance.rs (L4594-4597)
```rust
        }

        neuron
            .check_principal_authorized_to_change_permissions(caller, permissions_to_add.clone())?;
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

**File:** rs/sns/governance/src/neuron.rs (L782-786)
```rust
        // If there are no remaining permissions after removing the requested permissions, remove
        // the NeuronPermission entry from the neuron.
        if remaining_permission_types.is_empty() {
            self.permissions.swap_remove(existing_permission_position);
            return Ok(RemovePermissionsStatus::AllPermissionTypesRemoved);
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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L3091-3094)
```rust
    /// Remove a set of permissions from the Neuron for a given PrincipalId. If the PrincipalId has all of
    /// its permissions removed, it will be removed from the neuron's permissions list. This is a dangerous
    /// operation as it's possible to remove all permissions for a neuron and no longer be able to modify
    /// its state, i.e. disbursing the neuron back into the governance token.
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
