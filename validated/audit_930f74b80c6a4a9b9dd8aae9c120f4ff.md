### Title
Principal with `ManagePrincipals` Can Permanently Lock Staked SNS Tokens by Removing All `Disburse` Permissions - (`File: rs/sns/governance/src/governance.rs`)

---

### Summary

In the SNS Governance canister, any principal holding `ManagePrincipals` permission on a neuron can call `manage_neuron` → `RemoveNeuronPermissions` to strip all permissions — including `Disburse` — from every other principal. Once no principal retains `Disburse` permission, the neuron's staked SNS tokens are permanently locked: `disburse_neuron` enforces a hard `check_authorized(caller, NeuronPermissionType::Disburse)` gate with no fallback. The code itself acknowledges this danger in a comment but provides no guard against it.

---

### Finding Description

`disburse_neuron` in `rs/sns/governance/src/governance.rs` requires the caller to hold `NeuronPermissionType::Disburse`:

```rust
neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;
``` [1](#0-0) 

`check_authorized` returns `NotAuthorized` if the caller's principal is absent from the neuron's permission list with that type:

```rust
pub(crate) fn check_authorized(&self, principal: &PrincipalId, permission: NeuronPermissionType)
    -> Result<(), GovernanceError> {
    if !self.is_authorized(principal, permission) { return Err(...) }
    Ok(())
}
``` [2](#0-1) 

`remove_neuron_permissions` allows any caller holding `ManagePrincipals` to remove **any** set of permissions from **any** principal on the neuron, including removing `Disburse` from the last principal that holds it:

```rust
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
``` [3](#0-2) 

The authorization check only verifies the caller has `ManagePrincipals`; it does **not** verify that the resulting neuron state will still have at least one principal capable of disbursing:

```rust
let sufficient_permissions = if permissions_to_change.is_exclusively_voting_related() {
    vec![NeuronPermissionType::ManagePrincipals, NeuronPermissionType::ManageVotingPermission]
} else {
    vec![NeuronPermissionType::ManagePrincipals]
};
``` [4](#0-3) 

The code's own documentation acknowledges the danger but provides no enforcement:

> "This is a dangerous operation as it is possible to remove all permissions for a neuron and no longer be able to modify its state, i.e. disbursing the neuron back into the governance token." [5](#0-4) 

The `NeuronPermissionType::Disburse` variant is the exclusive gate for token withdrawal: [6](#0-5) 

---

### Impact Explanation

Once `Disburse` is removed from all principals on a neuron, the staked SNS tokens held in that neuron's ledger subaccount become permanently inaccessible. No ingress call can succeed on `disburse_neuron` because `check_authorized` will always return `NotAuthorized`. The neuron is not deleted — it persists in governance state holding a non-zero `cached_neuron_stake_e8s` — but the tokens can never be recovered. This is a permanent, irreversible loss of user funds proportional to the neuron's stake.

---

### Likelihood Explanation

The attack requires the victim to have granted `ManagePrincipals` to a second principal. This is a realistic and common pattern: multi-sig neuron management, protocol-level delegation, SNS DAO tooling, or a user granting a hot key `ManagePrincipals` for convenience. Once that grant is made, the second principal can immediately execute the attack via a single `manage_neuron` ingress call with `RemoveNeuronPermissions`. No privileged operator access, no governance majority, and no threshold key is required — only a standard user-level ingress message from the second principal.

---

### Recommendation

In `remove_neuron_permissions`, after computing the post-removal permission state, add an invariant check that at least one principal retains `NeuronPermissionType::Disburse`. If the removal would leave the neuron with zero `Disburse`-capable principals, return an error. Analogously, the same check should apply to `DisburseMaturity`. This mirrors the M-15 recommendation of making the depositor role immutable — here, the equivalent is ensuring the `Disburse` permission can never be fully revoked from a neuron.

---

### Proof of Concept

1. User A stakes SNS tokens and claims a neuron. By default, `neuron_claimer_permissions` grants User A all permissions including `Disburse` and `ManagePrincipals`.

2. User A grants User B `ManagePrincipals` via `AddNeuronPermissions` (a common delegation pattern).

3. User B sends an ingress `manage_neuron` call:
   ```
   ManageNeuron {
     subaccount: <neuron_subaccount>,
     command: RemoveNeuronPermissions {
       principal_id: <User_A_principal>,
       permissions_to_remove: NeuronPermissionList::all(),  // includes Disburse
     }
   }
   ```
   This succeeds because User B holds `ManagePrincipals`. [7](#0-6) 

4. User A's neuron now has zero principals with `Disburse`. User A calls `manage_neuron` → `Disburse`. The call reaches:
   ```rust
   neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;
   ```
   and returns `NotAuthorized`. [1](#0-0) 

5. User A's staked SNS tokens are permanently locked. No recovery path exists in the protocol.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1126-1127)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L4694-4705)
```rust
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
```

**File:** rs/sns/governance/src/neuron.rs (L104-122)
```rust
    pub(crate) fn check_authorized(
        &self,
        principal: &PrincipalId,
        permission: NeuronPermissionType,
    ) -> Result<(), GovernanceError> {
        if !self.is_authorized(principal, permission) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "Caller '{:?}' is not authorized to perform action: '{:?}' on neuron '{}'.",
                    principal,
                    permission,
                    self.id.as_ref().expect("Neuron must have a NeuronId"),
                ),
            ));
        }

        Ok(())
    }
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

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L4304-4305)
```rust
    /// The principal has permission to disburse the neuron.
    Disburse = 5,
```

**File:** rs/sns/governance/tests/governance.rs (L919-951)
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
```
