### Title
`ManageVotingPermission` Role Silently Blocked by `neuron_grantable_permissions` Guard in SNS `add_neuron_permissions` — (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

In the SNS governance canister, `add_neuron_permissions` enforces two sequential authorization checks. The first check correctly allows a caller holding `ManageVotingPermission` to add voting-related permissions. The second check — `check_permissions_are_grantable` — validates the requested permissions against `NervousSystemParameters::neuron_grantable_permissions`. That parameter is explicitly documented as the set that a `ManagePrincipals` holder may grant, yet the code applies it uniformly to `ManageVotingPermission` callers as well. If an SNS governance proposal narrows `neuron_grantable_permissions` to exclude voting permissions, every principal holding only `ManageVotingPermission` silently loses the ability to grant voting permissions — the exact function the role was designed to perform — while the first authorization check still passes, giving a false sense of success.

---

### Finding Description

`add_neuron_permissions` in `rs/sns/governance/src/governance.rs` executes two independent guards in sequence:

**Guard 1** — caller authorization: [1](#0-0) 

When the requested permissions are exclusively voting-related (`Vote`, `SubmitProposal`, `ManageVotingPermission`), either `ManagePrincipals` **or** `ManageVotingPermission` is accepted. [2](#0-1) 

**Guard 2** — grantability check: [3](#0-2) 

This check validates the requested permissions against `neuron_grantable_permissions`: [4](#0-3) 

The parameter's own documentation states it governs what a **`ManagePrincipals`** holder may grant: [5](#0-4) 

The `ManageVotingPermission` type is defined as the right to grant/revoke voting-related permissions: [6](#0-5) 

`PERMISSIONS_RELATED_TO_VOTING` — the exact set `ManageVotingPermission` is meant to control — is: [7](#0-6) 

**The mismatch**: Guard 1 passes for a `ManageVotingPermission` caller requesting `Vote`/`SubmitProposal`. Guard 2 then rejects the call if those permissions are absent from `neuron_grantable_permissions`. Because `neuron_grantable_permissions` is a governance-controlled parameter scoped to `ManagePrincipals`, an SNS can legitimately restrict it (e.g., to prevent `ManagePrincipals` holders from granting voting rights) without intending to break `ManageVotingPermission` holders — yet both roles are silently broken by the same parameter.

Note that `remove_neuron_permissions` does **not** call `check_permissions_are_grantable`: [8](#0-7) 

This creates an asymmetry: `ManageVotingPermission` holders can always **remove** voting permissions but may be unable to **add** them, depending on `neuron_grantable_permissions`.

---

### Impact Explanation

A principal holding only `ManageVotingPermission` on an SNS neuron calls `manage_neuron → AddNeuronPermissions` to grant `Vote` to a delegate. Guard 1 passes. Guard 2 fails with `ErrorType::AccessControlList` if `Vote` is absent from `neuron_grantable_permissions`. The role is non-functional. In a scenario where an SNS governance proposal tightens `neuron_grantable_permissions` (e.g., to prevent `ManagePrincipals` from granting voting rights to arbitrary principals), `ManageVotingPermission` holders are collaterally locked out of their sole purpose. Voting delegation management for the affected neurons becomes impossible without a further governance proposal — a governance deadlock analogous to M-14.

---

### Likelihood Explanation

The default `neuron_grantable_permissions` in `NervousSystemParameters::with_default_values()` is an empty list: [9](#0-8) 

SNS instances initialized via the swap set it to all permissions: [10](#0-9) 

However, any subsequent `ManageNervousSystemParameters` proposal can narrow this set. An SNS community may do so to restrict `ManagePrincipals` from granting voting rights, not realizing the same restriction silently disables `ManageVotingPermission` holders. The trigger is a normal, unprivileged governance action — no malicious majority is required; an inadvertent proposal suffices.

---

### Recommendation

Decouple the grantability check by role. When the caller is authorized via `ManageVotingPermission` (not `ManagePrincipals`), skip `check_permissions_are_grantable` for permissions that are exclusively voting-related, since `neuron_grantable_permissions` is semantically scoped to `ManagePrincipals`. Alternatively, introduce a separate `voting_grantable_permissions` parameter that governs what `ManageVotingPermission` holders may grant, keeping the two roles independently configurable. At minimum, update the documentation of `neuron_grantable_permissions` to explicitly state it also constrains `ManageVotingPermission` callers, so SNS governance is aware of the cross-role effect before narrowing the set.

---

### Proof of Concept

1. An SNS is deployed with `neuron_grantable_permissions = [Disburse, Split]` (voting permissions excluded — a plausible governance decision to prevent `ManagePrincipals` from granting voting rights).
2. Principal **A** holds `ManageVotingPermission` on neuron **N** (granted at neuron creation or by a prior `ManagePrincipals` holder when the parameter was broader).
3. **A** submits `manage_neuron { subaccount: N, command: AddNeuronPermissions { principal_id: B, permissions_to_add: [Vote] } }`.
4. Guard 1 (`check_principal_authorized_to_change_permissions`): `Vote` is voting-related → `ManageVotingPermission` is sufficient → **passes**. [11](#0-10) 
5. Guard 2 (`check_permissions_are_grantable`): `Vote` ∉ `neuron_grantable_permissions` → **fails** with `AccessControlList` error. [12](#0-11) 
6. **A**'s `ManageVotingPermission` role is non-functional. No voting delegation can be added to neuron **N** without a new governance proposal to expand `neuron_grantable_permissions` — which itself requires a `ManagePrincipals` holder or governance majority, creating a potential deadlock.

### Citations

**File:** rs/sns/governance/src/neuron.rs (L61-65)
```rust
    pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
        NeuronPermissionType::Vote,
        NeuronPermissionType::SubmitProposal,
        NeuronPermissionType::ManageVotingPermission,
    ];
```

**File:** rs/sns/governance/src/neuron.rs (L144-165)
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

```

**File:** rs/sns/governance/src/governance.rs (L4599-4600)
```rust
        self.nervous_system_parameters_or_panic()
            .check_permissions_are_grantable(permissions_to_add)?;
```

**File:** rs/sns/governance/src/governance.rs (L4694-4697)
```rust
        neuron.check_principal_authorized_to_change_permissions(
            caller,
            permissions_to_remove.clone(),
        )?;
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L51-53)
```text
  // The principal has permission to grant/revoke permission to vote and submit
  // proposals on behalf of the neuron to other principals.
  NEURON_PERMISSION_TYPE_MANAGE_VOTING_PERMISSION = 10;
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1218-1223)
```text
  // The superset of neuron permissions a principal with permission
  // `NeuronPermissionType::ManagePrincipals` for a given neuron can grant to another
  // principal for this same neuron.
  // If this set changes via a ManageNervousSystemParameters proposal, previous
  // neurons' permissions will be unchanged and only newly granted permissions will be affected.
  optional NeuronPermissionList neuron_grantable_permissions = 16;
```

**File:** rs/sns/governance/api_helpers/src/lib.rs (L44-44)
```rust
        neuron_grantable_permissions: Some(NeuronPermissionList::default()),
```

**File:** rs/sns/init/src/lib.rs (L813-814)
```rust
            neuron_claimer_permissions: Some(all_permissions.clone()),
            neuron_grantable_permissions: Some(all_permissions),
```
