### Title
Missing Anonymous Principal Check in `add_neuron_permissions` Allows Neuron Permission Takeover - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The `add_neuron_permissions` function in SNS Governance does not check whether the `principal_id` being granted permissions is the anonymous principal (`2vxsx-fae`). Because any ingress message can be sent with the anonymous principal as the sender without a cryptographic signature, granting the anonymous principal `ManagePrincipals` permission on a neuron allows any unprivileged caller to subsequently remove the original owner's permissions and take over the neuron.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `add_neuron_permissions` function validates that `principal_id` is not `None`, but performs no check for the anonymous principal:

```rust
let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
    GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
    )
})?;
// No check: principal_id.is_anonymous()
...
self.get_neuron_result_mut(neuron_id)?
    .add_permissions_for_principal(principal_id, permissions_to_add.permissions.clone());
``` [1](#0-0) 

The `is_authorized` check in `rs/sns/governance/src/neuron.rs` simply looks up the caller's principal in the neuron's permissions list:

```rust
let found_neuron_permission = self
    .permissions
    .iter()
    .find(|neuron_permission| neuron_permission.principal == Some(*principal));
``` [2](#0-1) 

Because the IC protocol allows any party to send an ingress message with `PrincipalId::new_anonymous()` as the sender (no signature required), once the anonymous principal holds `ManagePrincipals` on a neuron, every unprivileged caller on the internet can exercise that permission.

A parallel gap exists in `validate_fallback_controller_principal_ids` in `rs/sns/init/src/lib.rs` and `validate_principal` in `rs/sns/swap/src/types.rs`: neither rejects the anonymous principal, so it can be stored as a fallback controller. However, that path requires an NNS governance proposal to pass and is therefore privileged. [3](#0-2) [4](#0-3) 

### Impact Explanation
A neuron owner who accidentally (or through a UI bug) calls `AddNeuronPermissions` with `principal_id = anonymous` and includes `ManagePrincipals` in the permission list loses exclusive control of their neuron. Any anonymous ingress sender can then:

1. Call `manage_neuron → RemoveNeuronPermissions` to strip the original owner's permissions.
2. Call `manage_neuron → AddNeuronPermissions` to install themselves as the new controller.
3. Disburse, split, follow, or vote with the neuron's staked tokens and maturity.

The neuron's staked SNS tokens and accumulated maturity are permanently at risk.

### Likelihood Explanation
Likelihood is **Low**. The neuron owner must first issue the erroneous `AddNeuronPermissions` call. However, the missing guard is a footgun: no client-side or on-chain validation prevents it, and a UI bug or copy-paste error could trigger it. Once the anonymous principal holds `ManagePrincipals`, exploitation is trivially easy for any internet user.

### Recommendation
Add an anonymous-principal guard at the top of `add_neuron_permissions` in `rs/sns/governance/src/governance.rs`:

```rust
if principal_id.is_anonymous() {
    return Err(GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "Cannot grant permissions to the anonymous principal",
    ));
}
```

Similarly, extend `validate_fallback_controller_principal_ids` in `rs/sns/init/src/lib.rs` and `validate_principal` in `rs/sns/swap/src/types.rs` to reject the anonymous principal.

### Proof of Concept

```
// Step 1 – neuron owner (accidentally) grants ManagePrincipals to anonymous
manage_neuron(ManageNeuron {
    subaccount: owner_subaccount,
    command: Some(Command::AddNeuronPermissions(AddNeuronPermissions {
        principal_id: Some(PrincipalId::new_anonymous()),   // "2vxsx-fae"
        permissions_to_add: Some(NeuronPermissionList {
            permissions: vec![NeuronPermissionType::ManagePrincipals as i32],
        }),
    })),
})

// Step 2 – any anonymous ingress sender strips the owner
manage_neuron(ManageNeuron {          // caller = anonymous principal (no key needed)
    subaccount: owner_subaccount,
    command: Some(Command::RemoveNeuronPermissions(RemoveNeuronPermissions {
        principal_id: Some(owner_principal),
        permissions_to_remove: Some(NeuronPermissionList::all()),
    })),
})

// Step 3 – attacker installs themselves
manage_neuron(ManageNeuron {          // caller = anonymous principal
    subaccount: owner_subaccount,
    command: Some(Command::AddNeuronPermissions(AddNeuronPermissions {
        principal_id: Some(attacker_principal),
        permissions_to_add: Some(NeuronPermissionList::all()),
    })),
})
```

After step 3 the attacker has full control of the neuron; the original owner has none.

### Citations

**File:** rs/sns/governance/src/governance.rs (L4602-4634)
```rust
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
```

**File:** rs/sns/governance/src/neuron.rs (L130-137)
```rust
        let found_neuron_permission = self
            .permissions
            .iter()
            .find(|neuron_permission| neuron_permission.principal == Some(*principal));

        if let Some(p) = found_neuron_permission {
            return p.permission_type.contains(&(permission as i32));
        }
```

**File:** rs/sns/init/src/lib.rs (L1107-1142)
```rust
        let (valid_principals, invalid_principals): (Vec<_>, Vec<_>) = self
            .fallback_controller_principal_ids
            .iter()
            .map(|principal_id_string| {
                (
                    principal_id_string,
                    PrincipalId::from_str(principal_id_string),
                )
            })
            .partition(|item| item.1.is_ok());

        if !invalid_principals.is_empty() {
            return Err(format!(
                "Error: One or more fallback_controller_principal_ids is not a valid principal id. \
                The follow principals are invalid: {:?}",
                invalid_principals
                    .into_iter()
                    .map(|pair| pair.0)
                    .collect::<Vec<_>>()
            ));
        }

        // At this point, all principals are valid. Dedupe the values
        let unique_principals: BTreeSet<_> = valid_principals
            .iter()
            .filter_map(|pair| pair.1.clone().ok())
            .collect();

        if unique_principals.len() != valid_principals.len() {
            return Err(
                "Error: Duplicate PrincipalIds found in fallback_controller_principal_ids"
                    .to_string(),
            );
        }

        Ok(())
```

**File:** rs/sns/swap/src/types.rs (L28-35)
```rust
pub fn validate_principal(p: &str) -> Result<(), String> {
    let _ = PrincipalId::from_str(p).map_err(|x| {
        format!(
            "Couldn't validate PrincipalId. String \"{p}\" could not be converted to PrincipalId: {x}"
        )
    })?;
    Ok(())
}
```
