### Title
Single-Step Node Provider Transfer in `do_update_node_operator_config_directly` Allows Permanent Loss of Node Operator Control - (File: rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs)

### Summary
The `do_update_node_operator_config_directly` function in the Registry canister allows a Node Provider to immediately and irrevocably reassign the `node_provider_principal_id` of a `NodeOperatorRecord` to any arbitrary principal in a single step, with no confirmation from the new principal. If the new principal is wrong (typo, uncontrolled address, burned address), the Node Provider permanently loses control over the node operator record and all associated nodes, with no recovery path available without a governance proposal.

### Finding Description
The function `do_update_node_operator_config_directly_` in `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs` performs a single-step transfer of the `node_provider_principal_id` field of a `NodeOperatorRecord`:

```rust
node_operator_record.node_provider_principal_id = node_provider_id.to_vec();
// ...
self.maybe_apply_mutation_internal(mutations);
```

The caller (current Node Provider) supplies a `new_node_provider_id` in the `UpdateNodeOperatorConfigDirectlyPayload`. The registry immediately overwrites the `node_provider_principal_id` field with the new value. There is no pending-transfer state, no acceptance step, and no confirmation from the new principal that it controls the target address.

The only validation performed is:
1. The caller must equal the current `node_provider_principal_id` (authorization check).
2. The new `node_provider_id` must not equal the `node_operator_id` (self-assignment check).
3. A rate-limit reservation is consumed.

No check verifies that the new principal is reachable, self-authenticating, or has acknowledged the transfer.

By contrast, the `migrate_node_operator_directly` function (also in the Registry canister) requires that the new operator already exists in the registry and belongs to the same Node Provider, providing a structural guard. The `do_update_node_operator_config_directly_` function has no such guard — it will happily write any arbitrary `PrincipalId` as the new owner.

### Impact Explanation
If a Node Provider mistypes or supplies an uncontrolled principal as `new_node_provider_id`:

- The `node_provider_principal_id` of the `NodeOperatorRecord` is immediately overwritten.
- The original Node Provider can no longer call `do_update_node_operator_config_directly` for that record (authorization check at line 59–65 will reject them).
- The original Node Provider can no longer call `do_migrate_node_operator_directly` for that record (authorization check at line 134–141 will reject them).
- All nodes associated with the operator record remain assigned to it, but the Node Provider has lost the ability to manage them without going through a governance proposal.
- Node rewards for those nodes will be directed to the wrong principal's reward account.
- The impact is permanent loss of control over node operator records and associated nodes, which is high-severity for the affected Node Provider.

### Likelihood Explanation
Low. This requires the Node Provider (an authenticated, registered entity) to make a mistake when calling `update_node_operator_config_directly`. However, this is a realistic operational error (typo in a principal ID, copy-paste error, use of a test key that is later discarded). The function is directly callable by any registered Node Provider as an ingress message to the Registry canister, making it reachable without any privileged access beyond being a registered Node Provider.

### Recommendation
Implement a two-step transfer pattern for `do_update_node_operator_config_directly_`, analogous to the two-step node ownership transfer used elsewhere in the IC:

1. **Step 1 (propose):** The current Node Provider calls `update_node_operator_config_directly` with the new `node_provider_id`. This stores a `pending_node_provider_id` in the `NodeOperatorRecord` but does not yet overwrite the active `node_provider_principal_id`.
2. **Step 2 (accept):** The new principal calls an `accept_node_operator_config` method, which moves `pending_node_provider_id` into `node_provider_principal_id`.

This ensures the new principal demonstrably controls the target address before the transfer is finalized, preventing permanent loss of control due to a typo or misconfiguration.

### Proof of Concept

A Node Provider with principal `NP_A` owns a `NodeOperatorRecord` for operator `NO_X`. They intend to transfer ownership to `NP_B` but accidentally supply `NP_C` (an uncontrolled address):

```
// Attacker-controlled entry path: ingress message to Registry canister
update_node_operator_config_directly({
    node_operator_id: Some(NO_X),
    node_provider_id: Some(NP_C),  // typo: intended NP_B
})
// Caller: NP_A
```

The function at line 59–65 confirms `NP_A == current node_provider_principal_id` ✓.
The function at line 83 immediately writes `NP_C` as the new `node_provider_principal_id`.

Now `NP_A` calls the same function again to correct the mistake:
```
update_node_operator_config_directly({
    node_operator_id: Some(NO_X),
    node_provider_id: Some(NP_B),  // correct target
})
// Caller: NP_A
```

The check at line 59–65 now fails: `NP_A != NP_C`. The call is rejected. `NP_A` has permanently lost control of `NO_X` and all its associated nodes. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L58-65)
```rust
        // 2. Make sure that the caller is authorized to make the requested changes to node_operator_record.
        if caller
            != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap()
        {
            return Err(format!(
                "Caller {caller} not equal to the node_provider_princpal_id for this record."
            ));
        }
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L83-93)
```rust
        node_operator_record.node_provider_principal_id = node_provider_id.to_vec();

        // 5. Set and execute the mutation
        let mutations = vec![RegistryMutation {
            mutation_type: registry_mutation::Type::Update as i32,
            key: node_operator_record_key,
            value: node_operator_record.encode_to_vec(),
        }];

        // Check invariants before applying mutations
        self.maybe_apply_mutation_internal(mutations);
```

**File:** rs/registry/canister/canister/canister.rs (L825-829)
```rust
#[candid_method(update, rename = "update_node_operator_config_directly")]
fn update_node_operator_config_directly_(payload: UpdateNodeOperatorConfigDirectlyPayload) {
    registry_mut().do_update_node_operator_config_directly(payload);
    recertify_registry();
}
```

**File:** rs/registry/canister/src/mutations/do_migrate_node_operator_directly.rs (L132-141)
```rust
        // The caller must be the owner of both of the node operator
        // records.
        if caller.to_vec() != old_node_operator_record.node_provider_principal_id {
            return Err(MigrateError::NotAuthorized {
                caller,
                expected: PrincipalId(Principal::from_slice(
                    &old_node_operator_record.node_provider_principal_id,
                )),
            });
        }
```
