### Title
Single-Step Node Provider Principal Transfer Without Confirmation - (`File: rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`)

### Summary
The `do_update_node_operator_config_directly` endpoint in the registry canister allows a node provider to immediately and irrevocably transfer the `node_provider_principal_id` of their node operator record in a single step, with no confirmation required from the new address. If an incorrect principal is supplied (e.g., a typo, a principal for which no private key exists, or a miscopied ID), the original node provider permanently loses all administrative control over their node operator record and all associated nodes.

### Finding Description
The function `do_update_node_operator_config_directly_` in `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs` performs the following sequence:

1. Verifies the caller equals the current `node_provider_principal_id`.
2. Immediately overwrites `node_operator_record.node_provider_principal_id` with the caller-supplied `node_provider_id`.
3. Commits the mutation to the registry with no pending/confirmation state. [1](#0-0) 

There is no `pending_node_provider_id` field, no two-step handshake, and no way to reverse the change once committed. The new `node_provider_id` is never required to demonstrate it controls the corresponding private key before the transfer takes effect.

This endpoint is exposed as a public ingress method on the registry canister, callable by any node provider principal directly from the internet. [2](#0-1) 

### Impact Explanation
If a node provider accidentally supplies an incorrect `node_provider_id` (e.g., a mistyped principal, a principal for which no private key is held, or a copy-paste error), the following become permanently inaccessible to the original node provider:

- The ability to call `update_node_operator_config_directly` again (the caller check now fails against the new, wrong principal).
- The ability to add or remove nodes associated with this node operator record.
- Any governance or registry operations gated on being the `node_provider_principal_id` for this record.

The node operator record and all associated nodes are effectively orphaned under an uncontrollable principal. Recovery requires a full NNS governance proposal, which is a slow, high-friction process.

### Likelihood Explanation
Node provider principal IDs are long, opaque, base32-encoded strings. Operators routinely copy-paste them from dashboards, configuration files, or command-line outputs. A single character error produces a syntactically valid but wrong principal. The endpoint is callable directly via ingress by any node provider, making this a realistic human-error scenario with no safety net. The IC ecosystem has multiple active node providers who manage their records directly.

### Recommendation
Implement a two-step transfer for `node_provider_principal_id`:

1. **Step 1 – Propose**: The current node provider calls a new `propose_node_provider_transfer(node_operator_id, new_node_provider_id)` endpoint. This stores `new_node_provider_id` as a `pending_node_provider_id` in the node operator record without changing the active `node_provider_principal_id`.

2. **Step 2 – Accept**: The `new_node_provider_id` principal calls `accept_node_provider_transfer(node_operator_id)`. Only then is `node_provider_principal_id` updated to the new value.

This mirrors the pattern recommended for Lender.sol and ensures that only a principal that demonstrably controls its private key can complete the transfer. If an incorrect address is used in step 1, the current node provider simply re-proposes with the correct address.

### Proof of Concept

**Current vulnerable flow:**

```
# Node provider (NP) calls registry canister directly via ingress
dfx canister call registry update_node_operator_config_directly \
  '(record {
    node_operator_id = opt principal "abc...";
    node_provider_id = opt principal "WRONG_PRINCIPAL_TYPO..."
  })'
# Registry immediately sets node_provider_principal_id = WRONG_PRINCIPAL_TYPO
# NP can no longer call this endpoint (caller check fails)
# NP has permanently lost control of their node operator record
```

The root cause is at: [3](#0-2) 

Line 83 (`node_operator_record.node_provider_principal_id = node_provider_id.to_vec()`) executes unconditionally after the caller check passes, with no pending state, no confirmation window, and no rollback path. The mutation is then committed to the registry at line 93 (`self.maybe_apply_mutation_internal(mutations)`), making it permanent.

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L21-31)
```rust
    pub fn do_update_node_operator_config_directly(
        &mut self,
        payload: UpdateNodeOperatorConfigDirectlyPayload,
    ) {
        self.do_update_node_operator_config_directly_(
            payload,
            dfn_core::api::caller(),
            now_system_time(),
        )
        .unwrap()
    }
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L58-93)
```rust
        // 2. Make sure that the caller is authorized to make the requested changes to node_operator_record.
        if caller
            != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap()
        {
            return Err(format!(
                "Caller {caller} not equal to the node_provider_princpal_id for this record."
            ));
        }

        // 3. Check Rate Limits
        let current_node_provider = caller;
        let reservation =
            self.try_reserve_capacity_for_node_provider_operation(now, current_node_provider, 1)?;

        // 4. Check that the Node Provider is not being set with the same ID as the Node Operator
        let node_provider_id = payload
            .node_provider_id
            .ok_or("No Node Provider specified in the payload".to_string())?;

        if node_provider_id == node_operator_id {
            return Err(format!(
                "The Node Operator ID cannot be the same as the Node Provider ID: {node_operator_id}"
            ));
        }

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
