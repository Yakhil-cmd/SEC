### Title
Missing Anonymous Principal Validation in `add_neuron_permissions` Allows Granting Neuron Control to Uncontrollable Principal - (File: rs/sns/governance/src/governance.rs)

### Summary
The `add_neuron_permissions` function in SNS Governance validates that `principal_id` is not `None`, but does not reject the anonymous principal (`2vxsx-fae`). A neuron owner who accidentally passes the anonymous principal as the target of `AddNeuronPermissions` will grant neuron governance rights to a principal that any unauthenticated IC caller can impersonate, effectively making the neuron's permissions publicly accessible and enabling any caller to subsequently strip the original owner's control.

### Finding Description
In `add_neuron_permissions` at `rs/sns/governance/src/governance.rs`, the function performs the following validation on the `principal_id` field of `AddNeuronPermissions`:

```rust
let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
    GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
    )
})?;
```

This guards against `None` but does not guard against `Some(PrincipalId::new_anonymous())`. The `AddNeuronPermissions` protobuf message defines `principal_id` as an optional field:

```protobuf
message AddNeuronPermissions {
  ic_base_types.pb.v1.PrincipalId principal_id = 1;
  NeuronPermissionList permissions_to_add = 2;
}
```

When a front-end omits the `principal_id` field or a user interacts with the canister directly and leaves the field at its default, some Candid/protobuf encoders will encode the field as the zero-byte principal, which decodes to the anonymous principal (`PrincipalId::new_anonymous()`). This is the IC's direct analog of Ethereum's zero address: it is a valid, parseable principal that no one holds a private key for, but that any unauthenticated ingress sender implicitly uses.

After the check passes, the anonymous principal is written into the neuron's permission list:

```rust
self.get_neuron_result_mut(neuron_id)?
    .add_permissions_for_principal(principal_id, permissions_to_add.permissions.clone());
``` [1](#0-0) [2](#0-1) 

The `AddNeuronPermissions` struct in the generated Rust code confirms `principal_id` is `Option<PrincipalId>`, meaning the anonymous principal is a valid non-`None` value that bypasses the only guard: [3](#0-2) 

No other check in `add_neuron_permissions` rejects the anonymous principal as a target. The caller authorization check (`check_principal_authorized_to_change_permissions`) validates the *caller*, not the *target*: [4](#0-3) 

### Impact Explanation
Once the anonymous principal holds `ManagePrincipals` permission on a neuron, any unauthenticated IC ingress sender (whose implicit sender identity is the anonymous principal) can call `manage_neuron` with `RemoveNeuronPermissions` targeting the original owner's principal and strip all of the owner's permissions. The neuron's governance rights are effectively burned: the original owner loses the ability to disburse, vote, split, or further manage the neuron. Even granting lesser permissions (e.g., `Vote`, `Disburse`) to the anonymous principal allows any unauthenticated user to vote with or drain the neuron's staked tokens.

The proto comment on `RemoveNeuronPermissions` explicitly acknowledges this danger:

> "This is a dangerous operation as it's possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token." [5](#0-4) 

### Likelihood Explanation
The `principal_id` field is optional in the protobuf schema. Front-ends that render a form for `AddNeuronPermissions` may leave the field blank when the user does not fill it in, causing the Candid encoder to serialize it as the anonymous principal rather than as `None`. This is the exact pattern described in the reference report: a missing address parameter being interpreted as the zero/default value. The existing test `test_add_neuron_permission_missing_principal_id_fails` only covers the `None` case, not the anonymous-principal case, confirming the gap is untested and unguarded. [6](#0-5) 

### Recommendation
Add an explicit rejection of the anonymous principal immediately after the `None` check in `add_neuron_permissions`:

```rust
let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
    GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
    )
})?;

if principal_id == PrincipalId::new_anonymous() {
    return Err(GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "AddNeuronPermissions command must not target the anonymous principal",
    ));
}
```

The same guard should be applied to `remove_neuron_permissions` for symmetry, and a corresponding test should be added alongside `test_add_neuron_permission_missing_principal_id_fails`. [7](#0-6) 

### Proof of Concept
1. Neuron owner holds `ManagePrincipals` on neuron `N`.
2. Owner (or a front-end on their behalf) sends an ingress `manage_neuron` call with:
   ```
   ManageNeuron {
     subaccount: <neuron N's subaccount>,
     command: AddNeuronPermissions({
       principal_id: Some(PrincipalId::new_anonymous()),  // "2vxsx-fae"
       permissions_to_add: [ManagePrincipals]
     })
   }
   ```
3. `add_neuron_permissions` accepts the call: `principal_id` is `Some(...)` so the `None` guard passes; no anonymous check exists.
4. The anonymous principal is written into neuron `N`'s permission list with `ManagePrincipals`.
5. Any unauthenticated caller now sends (without any key):
   ```
   ManageNeuron {
     subaccount: <neuron N's subaccount>,
     command: RemoveNeuronPermissions({
       principal_id: Some(<original owner's principal>),
       permissions_to_remove: [all permissions]
     })
   }
   ```
6. The original owner's permissions are removed. The owner can no longer disburse, vote, or manage neuron `N`. [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4570-4643)
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

        self.nervous_system_parameters_or_panic()
            .check_permissions_are_grantable(permissions_to_add)?;

        let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
            )
        })?;

        let existing_permissions = neuron
            .permissions
            .iter()
            .find(|permission| permission.principal == Some(principal_id));

        let max_number_of_principals_per_neuron = self
            .nervous_system_parameters_or_panic()
            .max_number_of_principals_per_neuron
            .expect("NervousSystemParameters.max_number_of_principals_per_neuron must be present");

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

        GovernanceProto::add_neuron_to_principal_in_principal_to_neuron_ids_index(
            &mut self.principal_to_neuron_ids_index,
            neuron_id,
            &principal_id,
        );

        Ok(())
    }
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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L3083-3090)
```rust
    pub struct AddNeuronPermissions {
        /// The PrincipalId that the permissions will be granted to.
        #[prost(message, optional, tag = "1")]
        pub principal_id: ::core::option::Option<::ic_base_types::PrincipalId>,
        /// The set of permissions that will be granted to the PrincipalId.
        #[prost(message, optional, tag = "2")]
        pub permissions_to_add: ::core::option::Option<super::NeuronPermissionList>,
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

**File:** rs/sns/integration_tests/src/neuron.rs (L2145-2209)
```rust
#[test]
fn test_add_neuron_permission_missing_principal_id_fails() {
    local_test_on_sns_subnet(|runtime| async move {
        let user = Sender::from_keypair(&TEST_USER1_KEYPAIR);
        let account_identifier = Account {
            owner: user.get_principal_id().0,
            subaccount: None,
        };
        let alloc = Tokens::from_tokens(1000).unwrap();

        let system_params = NervousSystemParameters {
            neuron_grantable_permissions: Some(NeuronPermissionList {
                permissions: NeuronPermissionType::all(),
            }),
            // ManagePrincipals will be granted to the claimer automatically
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
        // The neuron should have a single NeuronPermission after claiming
        assert_eq!(neuron.permissions.len(), 1);

        // Adding a new permission without specifying a PrincipalId should fail
        let add_neuron_permissions = AddNeuronPermissions {
            principal_id: None,
            permissions_to_add: Some(NeuronPermissionList {
                permissions: vec![NeuronPermissionType::Vote as i32],
            }),
        };

        let manage_neuron_response: ManageNeuronResponse = sns_canisters
            .governance
            .update_from_sender(
                "manage_neuron",
                candid_one,
                ManageNeuron {
                    subaccount: subaccount.to_vec(),
                    command: Some(Command::AddNeuronPermissions(add_neuron_permissions)),
                },
                &user,
            )
            .await
            .expect("Error calling manage_neuron");

        let error = match manage_neuron_response.command.unwrap() {
            CommandResponse::AddNeuronPermission(_) => {
                panic!("AddNeuronPermission should have errored")
            }
            CommandResponse::Error(error) => error,
            response => panic!("Unexpected response from manage_neuron: {response:?}"),
        };

        assert_eq!(error.error_type, ErrorType::InvalidCommand as i32);

        Ok(())
    });
```
