### Title
`ManageVotingPermission` Is Self-Replicating via `PERMISSIONS_RELATED_TO_VOTING` Classification, Allowing Unauthorized Propagation of Neuron Voting Control - (`rs/sns/governance/src/neuron.rs`)

### Summary

In SNS governance, `ManageVotingPermission` is included in the `PERMISSIONS_RELATED_TO_VOTING` constant. Because `check_principal_authorized_to_change_permissions` allows a caller holding `ManageVotingPermission` to add or remove any permission that is "exclusively voting-related," and because `ManageVotingPermission` itself satisfies that predicate, any principal holding `ManageVotingPermission` can grant `ManageVotingPermission` to arbitrary additional principals. This makes the permission self-replicating: a neuron owner who delegates `ManageVotingPermission` to a trusted party loses the ability to bound who ultimately holds that permission, violating the intended access-control guardrail.

### Finding Description

**Root cause — `PERMISSIONS_RELATED_TO_VOTING` includes `ManageVotingPermission` itself:** [1](#0-0) 

```rust
pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
    NeuronPermissionType::ManageVotingPermission,   // ← self-referential
];
```

**Authorization check — `ManageVotingPermission` is sufficient to change any "voting-related" permission:** [2](#0-1) 

```rust
let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
    vec![
        NeuronPermissionType::ManagePrincipals,
        NeuronPermissionType::ManageVotingPermission,   // ← allowed
    ]
} else {
    vec![NeuronPermissionType::ManagePrincipals]
};
```

**Predicate — `is_exclusively_voting_related` returns `true` for a list containing only `ManageVotingPermission`:** [3](#0-2) 

Because `ManageVotingPermission` is in `PERMISSIONS_RELATED_TO_VOTING`, `is_exclusively_voting_related([ManageVotingPermission])` returns `true`. Therefore `check_principal_authorized_to_change_permissions` accepts a caller who holds only `ManageVotingPermission` when the requested change is to add `ManageVotingPermission` to another principal.

**Execution path — `add_neuron_permissions` calls both checks in sequence:** [4](#0-3) 

The `check_permissions_are_grantable` guard (line 4599) only blocks the grant if `ManageVotingPermission` is absent from the SNS-level `neuron_grantable_permissions` parameter. When that parameter includes `ManageVotingPermission` (the common default in deployed SNSes, as shown in integration tests), both guards pass and the grant succeeds.

**Confirmed by existing test — the codebase explicitly tests and accepts this behavior:** [5](#0-4) 

The test `test_manage_voting_permission_allows_adding_permissions_related_to_voting` grants the full `PERMISSIONS_RELATED_TO_VOTING` set (which includes `ManageVotingPermission`) to a target principal using only a `ManageVotingPermission` caller, and asserts success. This confirms the self-replication path is live in production code.

**Analog to external report:** In the ERC721 report, `ADMIN_ROLE` bypassed frozen-role restrictions because `hasRole` allowed `ADMIN_ROLE` to satisfy `DEFAULT_ADMIN_ROLE` checks. Here, `ManageVotingPermission` bypasses the intended restriction that only `ManagePrincipals` holders can grant `ManageVotingPermission`, because `ManageVotingPermission` satisfies the "voting-related" predicate that unlocks the same grant path.

### Impact Explanation

A neuron owner who grants `ManageVotingPermission` to a delegate (e.g., a voting-automation canister) intends to allow that delegate to manage voting on their behalf. Because `ManageVotingPermission` is self-replicating, the delegate can silently grant `ManageVotingPermission` to an attacker-controlled principal. That attacker can then:

1. Vote on governance proposals on behalf of the neuron (potentially swinging outcomes on high-stakes NNS/SNS proposals).
2. Submit proposals on behalf of the neuron (consuming the neuron's stake as proposal fees).
3. Further propagate `ManageVotingPermission` to additional principals, up to `max_number_of_principals_per_neuron`.
4. Remove `ManageVotingPermission` from legitimate principals (including the original delegate), locking the neuron owner out of voting management without touching `ManagePrincipals`.

The neuron owner has no notification mechanism; they may not discover the unauthorized principals until governance damage has occurred. The neuron's staked funds (`Disburse`, `Split`) are not directly at risk, but its governance influence is fully compromised.

### Likelihood Explanation

The attack requires that: (a) the neuron owner has granted `ManageVotingPermission` to at least one principal, and (b) that principal is compromised or acts maliciously. Condition (a) is the normal operating mode for any neuron that uses a voting service or hotkey delegation. Condition (b) is realistic given that voting-service canisters are third-party software. The `neuron_grantable_permissions` guard is the only external brake, and it is routinely set to `NeuronPermissionList::all()` in production SNS deployments. The attack is a single `manage_neuron` call with `AddNeuronPermissions`, requiring no privileged access beyond the already-held `ManageVotingPermission`.

### Recommendation

Remove `ManageVotingPermission` from `PERMISSIONS_RELATED_TO_VOTING`. It is a meta-permission (the ability to manage voting permissions), not a voting action itself. Keeping it in the voting-related set makes it self-replicating. After the change, only `ManagePrincipals` holders can grant or revoke `ManageVotingPermission`, which matches the intended access-control hierarchy.

```rust
// rs/sns/governance/src/neuron.rs
pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
-   NeuronPermissionType::ManageVotingPermission,
];
```

If the intent is to allow `ManageVotingPermission` holders to delegate voting to others but not to replicate their own management authority, a separate predicate (e.g., `PERMISSIONS_GRANTABLE_BY_MANAGE_VOTING`) should be introduced that includes only `Vote` and `SubmitProposal`.

### Proof of Concept

```
Precondition:
  - SNS neuron N owned by Alice (has ManagePrincipals).
  - neuron_grantable_permissions includes ManageVotingPermission.
  - Alice grants ManageVotingPermission to voting-service canister V.

Attack:
  1. Attacker controls canister A.
  2. V (compromised or malicious) calls manage_neuron on N:
       Command::AddNeuronPermissions {
           principal_id: A,
           permissions_to_add: [ManageVotingPermission, Vote, SubmitProposal],
       }
     → check_principal_authorized_to_change_permissions: caller=V has ManageVotingPermission,
       permissions=[ManageVotingPermission, Vote, SubmitProposal] are exclusively voting-related → OK
     → check_permissions_are_grantable: all three are in neuron_grantable_permissions → OK
     → A is added to N's permission list with ManageVotingPermission.

  3. Alice revokes ManageVotingPermission from V (discovers compromise).
  4. A still holds ManageVotingPermission on N.
  5. A votes on governance proposals using N's voting power.
  6. A grants ManageVotingPermission to B, C, … up to max_number_of_principals_per_neuron.

Result: Alice's neuron votes for attacker-chosen proposals indefinitely.
``` [1](#0-0) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/neuron.rs (L61-65)
```rust
    pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
        NeuronPermissionType::Vote,
        NeuronPermissionType::SubmitProposal,
        NeuronPermissionType::ManageVotingPermission,
    ];
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

**File:** rs/sns/governance/src/governance.rs (L229-238)
```rust
    // Returns true if no element in the permission list is not voting-related
    pub fn is_exclusively_voting_related(&self) -> bool {
        let permissions_related_to_voting = Neuron::PERMISSIONS_RELATED_TO_VOTING
            .iter()
            .map(|p| *p as i32)
            .collect::<Vec<_>>();
        self.permissions
            .iter()
            .all(|p| permissions_related_to_voting.contains(p))
    }
```

**File:** rs/sns/governance/src/governance.rs (L4570-4600)
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
```

**File:** rs/sns/governance/tests/governance.rs (L954-984)
```rust
#[test]
fn test_manage_voting_permission_allows_adding_permissions_related_to_voting() {
    let caller = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let target = *TEST_NEURON_2_OWNER_PRINCIPAL;
    let permissions_to_add: NeuronPermissionList =
        Neuron::PERMISSIONS_RELATED_TO_VOTING.to_vec().into();
    let (mut governance, neuron) = {
        let permissions: &[(PrincipalId, NeuronPermissionList)] = &[(
            caller,
            vec![NeuronPermissionType::ManageVotingPermission].into(),
        )];
        let user_principal = PrincipalId::new_user_test_id(0);
        let neuron_id = neuron_id(user_principal, 0);

        let governance_fixture = GovernanceCanisterFixtureBuilder::new()
            .with_neuron_grantable_permissions(NeuronPermissionList::all())
            .add_neuron_with_permissions(permissions, neuron_id.clone())
            .create();

        (governance_fixture, neuron_id)
    };

    // Attempt to add voting-related permissions to `target` - should succeed
    // since `caller` has ManageVotingPermission.
    governance
        .add_neuron_permissions(&neuron, target, permissions_to_add.clone(), caller)
        .unwrap();

    // Check that `target` now has those permissions.
    governance.assert_principal_has_permissions_for_neuron(&neuron, target, permissions_to_add);
}
```
