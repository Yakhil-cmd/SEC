### Title
Unchecked Anonymous Principal in `add_neuron_permissions` Allows Permanent Neuron Lock - (File: rs/sns/governance/src/governance.rs)

### Summary
The `add_neuron_permissions` function in SNS Governance validates that the `principal_id` field is not `None`, but does not validate that it is not the IC anonymous principal (`PrincipalId::new_anonymous()`). Any neuron holder with `ManagePrincipals` permission can add the anonymous principal as a permission holder. If they subsequently remove all other principals from the neuron, the neuron becomes permanently uncontrollable — no one can ever authenticate as the anonymous principal to exercise those permissions — resulting in permanent loss of staked governance tokens.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `add_neuron_permissions` function extracts the `principal_id` from the `AddNeuronPermissions` command and rejects `None`, but performs no further validation:

```rust
let principal_id = add_neuron_permissions.principal_id.ok_or_else(|| {
    GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "AddNeuronPermissions command must provide a PrincipalId to add permissions to",
    )
})?;
```

After this check, `principal_id` is unconditionally written into the neuron's permission list:

```rust
self.get_neuron_result_mut(neuron_id)?
    .add_permissions_for_principal(principal_id, permissions_to_add.permissions.clone());
```

The anonymous principal (`2vxsx-fae`) is the IC equivalent of Ethereum's `address(0)` — it is a well-known, unauthenticated identity that no real user can sign messages as. Granting it neuron permissions is semantically equivalent to granting permissions to a null address.

The `validate_and_render_deregister_dapp_canisters` function in `rs/sns/governance/src/proposal.rs` has the same class of bug in `new_controllers` — it checks the list is non-empty but does not reject the anonymous principal — though that path requires a governance majority.

### Impact Explanation
A neuron holder with `ManagePrincipals` permission (a standard permission granted to neuron claimers by default) can:
1. Call `manage_neuron` → `AddNeuronPermissions` with `principal_id = PrincipalId::new_anonymous()` and any permission set.
2. Call `manage_neuron` → `RemoveNeuronPermissions` to remove all real principals from the neuron.

The neuron is now permanently locked: the anonymous principal holds all permissions but can never authenticate to exercise them. The staked tokens inside the neuron cannot be disbursed, dissolved, or transferred. This constitutes permanent, irreversible loss of funds for the neuron owner.

### Likelihood Explanation
The entry path is reachable by any SNS neuron holder with `ManagePrincipals` permission — a standard permission. The scenario is most likely to occur through accidental misuse (e.g., copy-paste error when specifying a principal, or confusion about the anonymous principal's identity) rather than deliberate self-harm. The two-step nature (add anonymous, then remove self) reduces the probability of accidental triggering, but the missing validation is a clear defensive gap that other IC subsystems explicitly guard against (e.g., `ckETH` minter's `validate_config` rejects the anonymous principal for `ledger_id`, and `get_btc_address` asserts the owner is non-anonymous).

### Recommendation
Add an explicit check in `add_neuron_permissions` rejecting the anonymous principal, mirroring the pattern used elsewhere in the codebase:

```rust
if principal_id == PrincipalId::new_anonymous() {
    return Err(GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "AddNeuronPermissions: principal_id must not be the anonymous principal",
    ));
}
```

Apply the same check in `validate_and_render_deregister_dapp_canisters` for each entry in `new_controllers`.

### Proof of Concept

**Root cause — missing anonymous check:** [1](#0-0) 

**The write that follows with no further guard:** [2](#0-1) 

**Contrast: ckETH minter explicitly rejects anonymous principal for a critical address field:** [3](#0-2) 

**Contrast: ckBTC minter asserts non-anonymous for owner:** [4](#0-3) 

**Same class of bug in `DeregisterDappCanisters` proposal validation (new_controllers not checked for anonymous):** [5](#0-4) 

**The `AddNeuronPermissions` struct showing `principal_id` is optional (proto-level), making the None-only guard insufficient:** [6](#0-5)

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

**File:** rs/sns/governance/src/governance.rs (L4632-4634)
```rust
        // Re-borrow the neuron mutably to update now that the preconditions have been met
        self.get_neuron_result_mut(neuron_id)?
            .add_permissions_for_principal(principal_id, permissions_to_add.permissions.clone());
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L151-155)
```rust
        if self.cketh_ledger_id == Principal::anonymous() {
            return Err(InvalidStateError::InvalidLedgerId(
                "ledger_id cannot be the anonymous principal".to_string(),
            ));
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/get_btc_address.rs (L31-35)
```rust
    assert_ne!(
        owner,
        Principal::anonymous(),
        "the owner must be non-anonymous"
    );
```

**File:** rs/sns/governance/src/proposal.rs (L1669-1671)
```rust
    if deregister_dapp_canisters.new_controllers.is_empty() {
        return Err("DeregisterDappControllers must specify the new controllers".to_string());
    }
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
