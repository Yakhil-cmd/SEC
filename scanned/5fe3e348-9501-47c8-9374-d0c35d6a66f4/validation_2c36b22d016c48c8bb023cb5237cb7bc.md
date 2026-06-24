The critical fact is confirmed at `neuron.rs:61-65`: `ManageVotingPermission` is itself a member of `PERMISSIONS_RELATED_TO_VOTING`, which makes the described unbounded delegation chain real and exploitable.

---

### Title
Unbounded `ManageVotingPermission` Self-Propagation via `add_neuron_permissions` — (`rs/sns/governance/src/neuron.rs`, `rs/sns/governance/src/governance.rs`)

### Summary
A principal holding only `ManageVotingPermission` on a neuron can grant `ManageVotingPermission` to an arbitrary third party, who can repeat the grant indefinitely. Because `ManageVotingPermission` is classified as a "voting-related" permission, the authorization check accepts it as sufficient to grant itself, violating the invariant that permission delegation must be bounded by the granting principal's own authority level.

### Finding Description

`PERMISSIONS_RELATED_TO_VOTING` is defined as:

```rust
// rs/sns/governance/src/neuron.rs:61-65
pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
    NeuronPermissionType::ManageVotingPermission,  // ← included
];
``` [1](#0-0) 

`is_exclusively_voting_related()` returns `true` for any list whose members are all in that set:

```rust
// rs/sns/governance/src/governance.rs:230-238
pub fn is_exclusively_voting_related(&self) -> bool {
    let permissions_related_to_voting = Neuron::PERMISSIONS_RELATED_TO_VOTING
        .iter().map(|p| *p as i32).collect::<Vec<_>>();
    self.permissions.iter().all(|p| permissions_related_to_voting.contains(p))
}
``` [2](#0-1) 

`check_principal_authorized_to_change_permissions` then accepts either `ManagePrincipals` **or** `ManageVotingPermission` as sufficient when the list is exclusively voting-related:

```rust
// rs/sns/governance/src/neuron.rs:152-159
let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
    vec![
        NeuronPermissionType::ManagePrincipals,
        NeuronPermissionType::ManageVotingPermission,
    ]
} else {
    vec![NeuronPermissionType::ManagePrincipals]
};
``` [3](#0-2) 

`add_neuron_permissions` calls this check and then writes the new permission if `neuron_grantable_permissions` also allows it: [4](#0-3) 

Because `[ManageVotingPermission]` passes `is_exclusively_voting_related()`, a caller holding only `ManageVotingPermission` is authorized to grant `ManageVotingPermission` to any principal. That new principal is then in an identical position and can repeat the grant without bound.

### Impact Explanation

- **Voting control hijack**: Any principal that receives `ManageVotingPermission` can immediately grant `Vote` and `SubmitProposal` to themselves or colluding principals, casting votes with the victim neuron.
- **Unbounded fan-out**: The permission can be propagated to an unlimited number of principals, permanently expanding the set of entities that can vote with or submit proposals on behalf of the neuron.
- **Irreversibility without `ManagePrincipals`**: The original neuron owner may not hold `ManagePrincipals` themselves (e.g., a swap-claimed neuron with only voting permissions), leaving no way to revoke the rogue grants.

### Likelihood Explanation

The attack requires only that:
1. A neuron exists with `ManageVotingPermission` granted to a principal the attacker controls (a common configuration for voting hotkeys).
2. `neuron_grantable_permissions` includes `ManageVotingPermission` (the default `NervousSystemParameters::with_default_values()` includes it).

Both conditions are routinely satisfied in production SNS deployments. The attacker submits a standard `manage_neuron` ingress call — no privileged access, no key compromise, no social engineering required.

### Recommendation

Remove `ManageVotingPermission` from `PERMISSIONS_RELATED_TO_VOTING`:

```rust
pub const PERMISSIONS_RELATED_TO_VOTING: &'static [NeuronPermissionType] = &[
    NeuronPermissionType::Vote,
    NeuronPermissionType::SubmitProposal,
    // ManageVotingPermission removed — granting it requires ManagePrincipals
];
```

This ensures that only a principal with `ManagePrincipals` can delegate `ManageVotingPermission`, while a principal with only `ManageVotingPermission` can still grant/revoke `Vote` and `SubmitProposal` as intended.

### Proof of Concept

```
1. SNS is configured with neuron_grantable_permissions = all.
2. neuron N has: principal A → [ManageVotingPermission]
3. A calls manage_neuron(N, AddNeuronPermissions { principal: B, permissions: [ManageVotingPermission] })
   → check_principal_authorized_to_change_permissions: [ManageVotingPermission].is_exclusively_voting_related() == true
   → A has ManageVotingPermission → authorized
   → check_permissions_are_grantable: ManageVotingPermission ∈ neuron_grantable_permissions → passes
   → B now has ManageVotingPermission on N
4. B calls manage_neuron(N, AddNeuronPermissions { principal: C, permissions: [ManageVotingPermission] })
   → same checks pass → C now has ManageVotingPermission on N
5. C grants Vote to itself → C votes with N's stake.
6. Steps 4-5 repeat for any number of additional principals.
```

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

**File:** rs/sns/governance/src/governance.rs (L230-238)
```rust
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
