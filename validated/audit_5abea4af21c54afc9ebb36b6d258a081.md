Audit Report

## Title
`ManageVotingPermission` Is Self-Replicating via `PERMISSIONS_RELATED_TO_VOTING`, Allowing Unbounded Propagation of Neuron Voting Control - (`rs/sns/governance/src/neuron.rs`)

## Summary

`ManageVotingPermission` is included in the `PERMISSIONS_RELATED_TO_VOTING` constant, and `check_principal_authorized_to_change_permissions` permits any caller holding `ManageVotingPermission` to add or remove any permission that `is_exclusively_voting_related()` returns `true` for. Because `ManageVotingPermission` itself satisfies that predicate, a principal holding only `ManageVotingPermission` can grant `ManageVotingPermission` to arbitrary additional principals. A neuron owner who delegates this permission to a third-party voting service loses the ability to bound who ultimately holds voting control over their neuron.

## Finding Description

**Root cause — `PERMISSIONS_RELATED_TO_VOTING` includes `ManageVotingPermission` itself:**

`rs/sns/governance/src/neuron.rs` lines 61–65:
```rust
pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
    NeuronPermissionType::ManageVotingPermission,  // self-referential
];
``` [1](#0-0) 

**`is_exclusively_voting_related` returns `true` for a list containing only `ManageVotingPermission`:**

`rs/sns/governance/src/governance.rs` lines 230–238 checks whether every element of the requested permission list is a member of `PERMISSIONS_RELATED_TO_VOTING`. Since `ManageVotingPermission` is in that set, `is_exclusively_voting_related([ManageVotingPermission])` returns `true`. [2](#0-1) 

**Authorization check — `ManageVotingPermission` is sufficient to change any "voting-related" permission:**

`rs/sns/governance/src/neuron.rs` lines 152–156: when `is_exclusively_voting_related()` is `true`, both `ManagePrincipals` and `ManageVotingPermission` are accepted as sufficient. There is no additional check preventing a `ManageVotingPermission` holder from granting `ManageVotingPermission` to a new principal. [3](#0-2) 

**Execution path — `add_neuron_permissions` calls both guards in sequence:**

`rs/sns/governance/src/governance.rs` lines 4596–4600: `check_principal_authorized_to_change_permissions` (line 4596) passes because the caller holds `ManageVotingPermission` and the requested permissions are voting-related. `check_permissions_are_grantable` (line 4599) passes when `neuron_grantable_permissions` includes `ManageVotingPermission`, which is the default in production SNS deployments (`NeuronPermissionList::all()`). No further guard exists. [4](#0-3) 

**Confirmed by existing test:**

`rs/sns/governance/tests/governance.rs` lines 954–984: `test_manage_voting_permission_allows_adding_permissions_related_to_voting` sets up a caller with only `ManageVotingPermission`, calls `add_neuron_permissions` with the full `PERMISSIONS_RELATED_TO_VOTING` set (which includes `ManageVotingPermission`) targeting a different principal, and asserts `.unwrap()` — confirming the self-replication path is live and tested. [5](#0-4) 

## Impact Explanation

A neuron owner who grants `ManageVotingPermission` to a third-party voting-service canister intends to delegate voting management to that specific principal. Because `ManageVotingPermission` is self-replicating, a compromised or malicious delegate can silently grant `ManageVotingPermission` to an attacker-controlled principal. That attacker can then: (1) vote on governance proposals using the neuron's full voting power, (2) submit proposals on behalf of the neuron consuming its stake as fees, (3) further propagate `ManageVotingPermission` to additional principals up to `max_number_of_principals_per_neuron`, and (4) remove `ManageVotingPermission` from legitimate principals including the original delegate. The neuron owner has no notification mechanism and may not discover the unauthorized principals until governance damage has occurred. Staked funds (`Disburse`, `Split`) are not directly at risk, but the neuron's governance influence is fully compromised. This matches the allowed High impact: **"Unauthorized access to neurons, governance assets … where exploitation requires meaningful per-target work or other constraints."**

## Likelihood Explanation

The attack requires: (a) the neuron owner has granted `ManageVotingPermission` to at least one principal — the normal operating mode for any neuron using a voting service or hotkey delegation — and (b) that principal is compromised or acts maliciously. Voting-service canisters are third-party software with their own upgrade and key-management risks. The `neuron_grantable_permissions` guard is the only external brake, and it is routinely set to `NeuronPermissionList::all()` in production SNS deployments as shown in integration tests. The attack is a single `manage_neuron` call with `AddNeuronPermissions`, requiring no privileged access beyond the already-held `ManageVotingPermission`.

## Recommendation

Remove `ManageVotingPermission` from `PERMISSIONS_RELATED_TO_VOTING`. It is a meta-permission (the ability to manage voting permissions), not a voting action itself. After the change, only `ManagePrincipals` holders can grant or revoke `ManageVotingPermission`, matching the intended access-control hierarchy:

```rust
// rs/sns/governance/src/neuron.rs
pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
-   NeuronPermissionType::ManageVotingPermission,
];
```

If the intent is to allow `ManageVotingPermission` holders to delegate voting to others but not replicate their own management authority, introduce a separate predicate (e.g., `PERMISSIONS_GRANTABLE_BY_MANAGE_VOTING`) containing only `Vote` and `SubmitProposal`, and use it in `check_principal_authorized_to_change_permissions` instead of `is_exclusively_voting_related`.

## Proof of Concept

The existing unit test `test_manage_voting_permission_allows_adding_permissions_related_to_voting` in `rs/sns/governance/tests/governance.rs` already constitutes a reproducible proof:

1. Caller is given only `ManageVotingPermission` on a neuron.
2. `neuron_grantable_permissions` is set to `NeuronPermissionList::all()`.
3. Caller calls `add_neuron_permissions` targeting a different principal with `PERMISSIONS_RELATED_TO_VOTING` (which includes `ManageVotingPermission`).
4. The call succeeds (`.unwrap()`), and the target now holds `ManageVotingPermission`.

To demonstrate the full attack chain, extend the test: have the newly-granted principal repeat step 3 targeting a third principal, confirm success, then have the original owner revoke the first delegate's `ManageVotingPermission`, and confirm the third principal still holds `ManageVotingPermission` and can vote — demonstrating the permission survives revocation of the original delegate.

### Citations

**File:** rs/sns/governance/src/neuron.rs (L61-65)
```rust
    pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
        NeuronPermissionType::Vote,
        NeuronPermissionType::SubmitProposal,
        NeuronPermissionType::ManageVotingPermission,
    ];
```

**File:** rs/sns/governance/src/neuron.rs (L152-159)
```rust
        let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
            vec![
                NeuronPermissionType::ManagePrincipals,
                NeuronPermissionType::ManageVotingPermission,
            ]
        } else {
            vec![NeuronPermissionType::ManagePrincipals]
        };
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

**File:** rs/sns/governance/src/governance.rs (L4596-4600)
```rust
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
