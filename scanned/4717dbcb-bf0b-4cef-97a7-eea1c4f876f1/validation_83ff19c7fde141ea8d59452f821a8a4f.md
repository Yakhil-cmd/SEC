### Title
SNS Governance Allows Granting Neuron Permissions to the Anonymous Principal - (File: rs/sns/governance/src/governance.rs)

### Summary
The `add_neuron_permissions` function in SNS Governance does not validate that the target `principal_id` is not the anonymous principal. Any neuron holder with `ManagePrincipals` (or `ManageVotingPermission`) can grant full neuron permissions to `PrincipalId::new_anonymous()`, after which any unauthenticated ingress sender can exercise those permissions — including voting, submitting proposals, and disbursing staked tokens.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `add_neuron_permissions` function validates that `principal_id` is not `None`, but performs no check that it is not the anonymous principal:

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
```

The `manage_neuron` canister endpoint accepts calls from any sender, including the anonymous principal. Once permissions are granted to the anonymous principal, any unauthenticated caller can invoke `manage_neuron` as the anonymous principal and exercise those permissions.

The analog to the external report is direct: just as `AlgebraFactory.sol` grants `DEFAULT_ADMIN_ROLE` to `address(0)` on ownership renouncement (making the role exercisable by no one but permanently locked), the IC analog is granting neuron permissions to the anonymous principal — which is exercisable by *everyone*, since the anonymous principal requires no authentication.

### Impact Explanation
Depending on which permissions are granted to the anonymous principal:

- `Vote` / `ManageVotingPermission`: Any unauthenticated user can cast votes with the neuron, manipulating SNS governance outcomes.
- `SubmitProposal`: Any unauthenticated user can submit proposals on behalf of the neuron, burning its stake and spamming governance.
- `Disburse` / `DisburseMaturity`: Any unauthenticated user can drain the neuron's staked tokens to an arbitrary account.
- `ManagePrincipals`: Any unauthenticated user can add or remove permissions for all other principals, fully taking over the neuron's access control.

### Likelihood Explanation
A neuron owner with `ManagePrincipals` permission must explicitly supply the anonymous principal as the target of `AddNeuronPermissions`. This could occur:
- Accidentally, by a developer or user who passes a default/zero-value `PrincipalId` in a script or integration.
- Intentionally, as a "renounce control" pattern analogous to the AlgebraFactory bug — a user who wants to make a neuron "ownerless" but does not understand that the anonymous principal is callable by anyone.

The `manage_neuron` endpoint is publicly reachable via ingress from any user.

### Recommendation
Add an explicit check in `add_neuron_permissions` that the target `principal_id` is not the anonymous principal, mirroring the existing pattern used in `validate_and_render_transfer_sns_treasury_funds` and `validate_and_render_mint_sns_tokens`:

```rust
if principal_id.is_anonymous() {
    return Err(GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "AddNeuronPermissions: principal_id must not be anonymous.",
    ));
}
```

### Proof of Concept

**Entry path:**
1. Attacker or user owns a neuron with `ManagePrincipals` permission.
2. Calls `manage_neuron` → `AddNeuronPermissions { principal_id: Some(PrincipalId::new_anonymous()), permissions_to_add: Some(NeuronPermissionList::all()) }`.
3. `add_neuron_permissions` at line 4602 resolves `principal_id` as `Some(anonymous)` — the `ok_or_else` check passes because the value is `Some`, not `None`.
4. No further check rejects the anonymous principal.
5. `add_permissions_for_principal` at line 4634 stores the anonymous principal in the neuron's permission list.
6. Any subsequent unauthenticated caller sends `manage_neuron` → `Disburse` (or `Vote`, etc.) as the anonymous principal; `check_authorized` at line 4596 finds the anonymous principal in the permission list and allows the operation.

**Relevant code locations:**

`add_neuron_permissions` — missing anonymous check after resolving `principal_id`: [1](#0-0) 

`add_permissions_for_principal` — unconditionally stores the supplied principal: [2](#0-1) 

`manage_neuron` canister endpoint — no anonymous-caller guard: [3](#0-2) 

`is_authorized` — will match the anonymous principal if it is in the permission list: [4](#0-3) 

Existing pattern that correctly rejects the anonymous principal (not applied to `AddNeuronPermissions`): [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L4602-4607)
```rust
        let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
            )
        })?;
```

**File:** rs/sns/governance/src/neuron.rs (L125-140)
```rust
    pub(crate) fn is_authorized(
        &self,
        principal: &PrincipalId,
        permission: NeuronPermissionType,
    ) -> bool {
        let found_neuron_permission = self
            .permissions
            .iter()
            .find(|neuron_permission| neuron_permission.principal == Some(*principal));

        if let Some(p) = found_neuron_permission {
            return p.permission_type.contains(&(permission as i32));
        }

        false
    }
```

**File:** rs/sns/governance/src/neuron.rs (L4697-4727)
```rust

```

**File:** rs/sns/governance/canister/canister.rs (L397-408)
```rust
#[update]
async fn manage_neuron(request: ManageNeuron) -> ManageNeuronResponse {
    log!(INFO, "manage_neuron");
    let governance = governance_mut();
    let result = measure_span_async(
        governance.profiling_information,
        "manage_neuron",
        governance.manage_neuron(&sns_gov_pb::ManageNeuron::from(request), &caller()),
    )
    .await;
    ManageNeuronResponse::from(result)
}
```

**File:** rs/sns/governance/src/proposal.rs (L4357-4373)
```rust
    fn validate_and_render_transfer_sns_treasury_funds_anonymous_principal() {
        // invalid case anonymous principal
        assert_eq!(
            locally_validate_and_render_transfer_sns_treasury_funds(
                &TransferSnsTreasuryFunds {
                    from_treasury: TransferFrom::IcpTreasury.into(),
                    amount_e8s: 1000000,
                    memo: None,
                    to_principal: Some(PrincipalId::new_anonymous()),
                    to_subaccount: None
                },
                0,
                vec![],
            )
            .unwrap_err(),
            "TransferSnsTreasuryFunds proposal was invalid for the following reason(s):\nto_principal must not be anonymous.".to_string()
        );
```
