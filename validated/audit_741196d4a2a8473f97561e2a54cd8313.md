### Title
Excessive `ManagePrincipals` Privilege Enables Neuron Takeover via Permission Front-Running - (File: `rs/sns/governance/src/governance.rs`)

### Summary
In SNS governance, any principal holding `ManagePrincipals` on a neuron can grant that same permission to arbitrary principals and revoke it from all others, with no restriction. A compromised or malicious `ManagePrincipals` holder can front-run their own removal by delegating the permission to another address they control, and can strip all other co-principals of their permissions, achieving sole unauthorized control over the neuron's staked assets and voting power.

### Finding Description

The `add_neuron_permissions()` function in `rs/sns/governance/src/governance.rs` and `remove_neuron_permissions()` only verify that the caller holds `ManagePrincipals` via `check_principal_authorized_to_change_permissions()`. There is no restriction preventing a `ManagePrincipals` holder from granting `ManagePrincipals` to another principal they control, or from removing `ManagePrincipals` from all other co-principals.

**Root cause — `add_neuron_permissions`:**

The sole authorization check is:

```rust
neuron.check_principal_authorized_to_change_permissions(caller, permissions_to_add.clone())?;
self.nervous_system_parameters_or_panic()
    .check_permissions_are_grantable(permissions_to_add)?;
``` [1](#0-0) 

`check_principal_authorized_to_change_permissions` only checks whether the caller has `ManagePrincipals` (or `ManageVotingPermission` for voting-only changes): [2](#0-1) 

`check_permissions_are_grantable` only validates against the SNS-wide `neuron_grantable_permissions` list: [3](#0-2) 

`ManagePrincipals` is explicitly required to be in `neuron_claimer_permissions` and is part of the default grantable set: [4](#0-3) 

**Root cause — `remove_neuron_permissions`:**

The same `check_principal_authorized_to_change_permissions` is the only guard. There is no check preventing a `ManagePrincipals` holder from removing `ManagePrincipals` from all other principals: [5](#0-4) 

**Confirmed by tests:** The codebase explicitly tests and confirms that a `ManagePrincipals` holder can grant `ManagePrincipals` to any other principal (including themselves) and remove all permissions from any other principal: [6](#0-5) [7](#0-6) 

**Attacker-controlled entry path:**

1. Principal A and Principal B both hold `ManagePrincipals` on a shared SNS neuron (e.g., a team-managed neuron). Any user who stakes tokens and claims a neuron receives `ManagePrincipals` by default.
2. Principal A becomes compromised or malicious.
3. Principal B submits a `ManageNeuron { command: RemoveNeuronPermissions { principal_id: A, ... } }` update call to the SNS governance canister.
4. Principal A, observing the pending removal, submits `ManageNeuron { command: AddNeuronPermissions { principal_id: C, permissions: [ManagePrincipals, Disburse, ...] } }` — granting full control to a second address C they own — before B's call is processed.
5. Principal A also submits `RemoveNeuronPermissions` targeting Principal B, stripping B of all permissions.
6. Principal A/C now has sole `ManagePrincipals` and can exercise all neuron permissions.

The IC processes update calls sequentially per canister; the attacker simply needs to submit their calls before the victim's call is executed. There is no mempool race — the attacker monitors the canister's ingress queue or simply acts preemptively upon detecting any attempt to revoke their access.

### Impact Explanation

A compromised `ManagePrincipals` holder achieves permanent, irrevocable sole control over the neuron. Concrete consequences:

- **Token theft**: If `Disburse` or `DisburseMaturity` are in `neuron_grantable_permissions` (default), the attacker can drain the neuron's staked SNS tokens to an address they control.
- **Governance manipulation**: The attacker can vote on all SNS proposals with the neuron's full voting power, or submit malicious proposals.
- **Permanent lock-out**: All legitimate co-principals are stripped of permissions with no recovery path, since the only recovery mechanism is another `ManagePrincipals` holder — which the attacker has eliminated.

The impact is scoped to the individual neuron but can be significant: neurons may hold large staked positions and carry substantial voting power in SNS governance.

### Likelihood Explanation

**Medium.** The precondition is that a principal with `ManagePrincipals` is compromised or turns malicious. This is realistic for:
- Shared/team-managed neurons where multiple principals are granted `ManagePrincipals`
- Neurons where `ManagePrincipals` was delegated to a hot key or third-party service

Once the precondition is met, the attack requires only two standard `manage_neuron` update calls, which any principal can submit to the SNS governance canister without any special access.

### Recommendation

Enforce a "cannot grant what you do not own" invariant: when adding permissions, verify that the caller's own permission set is a superset of the permissions being granted. This prevents privilege escalation via delegation.

Additionally, restrict the ability to grant or revoke `ManagePrincipals` itself to a separate, higher-privilege role (analogous to the report's recommendation of an `owner` role), or require that at least one `ManagePrincipals` holder always remains after any removal operation.

### Proof of Concept

The following sequence, executable against any SNS governance canister where `ManagePrincipals` is in `neuron_grantable_permissions`:

```
// Step 1: Attacker (principal A) grants ManagePrincipals to their backup key C
manage_neuron({
  subaccount: <neuron_subaccount>,
  command: AddNeuronPermissions({
    principal_id: C,
    permissions_to_add: [ManagePrincipals, Disburse, Vote, SubmitProposal, ...]
  })
})  // called by A

// Step 2: Attacker strips co-principal B of all permissions
manage_neuron({
  subaccount: <neuron_subaccount>,
  command: RemoveNeuronPermissions({
    principal_id: B,
    permissions_to_remove: [ManagePrincipals, Vote, ...]
  })
})  // called by A

// Step 3: Attacker (now via C) disburses staked tokens
manage_neuron({
  subaccount: <neuron_subaccount>,
  command: Disburse({ to_account: attacker_account, amount: ... })
})  // called by C
```

Both Step 1 and Step 2 succeed because `check_principal_authorized_to_change_permissions` only verifies the caller holds `ManagePrincipals` [8](#0-7) 
and `check_permissions_are_grantable` only validates against the SNS-wide allowlist, not the caller's own permission set. [1](#0-0)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4596-4600)
```rust
        neuron
            .check_principal_authorized_to_change_permissions(caller, permissions_to_add.clone())?;

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

**File:** rs/sns/governance/src/types.rs (L943-973)
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

**File:** rs/sns/governance/tests/governance.rs (L919-952)
```rust
#[test]
fn test_removing_permissions_when_we_have_manage_principals() {
    let caller = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let target = *TEST_NEURON_2_OWNER_PRINCIPAL;
    let permissions_to_remove = NeuronPermissionList::all();
    let (mut governance, neuron) = {
        let permissions: &[(PrincipalId, NeuronPermissionList)] = &[
            (caller, vec![NeuronPermissionType::ManagePrincipals].into()),
            (target, permissions_to_remove.clone()),
        ];
        let user_principal = PrincipalId::new_user_test_id(0);
        let neuron_id = neuron_id(user_principal, 0);

        let governance_fixture = GovernanceCanisterFixtureBuilder::new()
            .with_neuron_grantable_permissions(NeuronPermissionList::all())
            .add_neuron_with_permissions(permissions, neuron_id.clone())
            .create();

        (governance_fixture, neuron_id)
    };

    // Attempt to remove permissions from `target` - should succeed since `caller`
    // has `ManagePrincipals`.
    governance
        .remove_neuron_permissions(&neuron, target, permissions_to_remove, caller)
        .unwrap();

    // Check that `target` now has no permissions.
    governance.assert_principal_has_permissions_for_neuron(
        &neuron,
        target,
        NeuronPermissionList::empty(),
    );
}
```
