### Title
Single-Step Node Provider Transfer Without Confirmation - (File: `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`)

### Summary
The `do_update_node_operator_config_directly` mutation in the registry canister allows a node provider to transfer their node operator record's `node_provider_principal_id` to a new principal in a single atomic step, with no confirmation required from the new principal. If a node provider supplies an incorrect `node_provider_id`, they immediately and irrevocably lose direct control of their node operator record.

### Finding Description
The function `do_update_node_operator_config_directly_` is callable directly by the current node provider (identified by `dfn_core::api::caller()`) without going through an NNS governance proposal. It validates that the caller matches the existing `node_provider_principal_id`, then unconditionally overwrites it with the caller-supplied `node_provider_id` in a single step: [1](#0-0) [2](#0-1) 

There is no pending-transfer state, no expiry, and no acceptance call required from the new `node_provider_id`. The mutation is applied immediately and written to the registry. The only guard against self-assignment is a check that `node_provider_id != node_operator_id`: [3](#0-2) 

No validation is performed to confirm that the new `node_provider_id` is a reachable or intended principal.

### Impact Explanation
A node provider who accidentally supplies a wrong principal (e.g., a typo, a stale clipboard value, or a misidentified principal text) in `node_provider_id` immediately loses the ability to call `update_node_operator_config_directly` for their own record. Recovery requires submitting an NNS governance proposal — a slow, public, and uncertain process. In the interim, the node provider cannot directly update their node allowance, rewardable nodes, or other operator metadata. All nodes whose `node_operator_id` points to the affected record remain operational but are now administratively orphaned from the original provider's direct control.

### Likelihood Explanation
Node providers interact with this endpoint directly as an ingress sender — no governance intermediary is required. Principal IDs are long opaque text strings; copy-paste errors, stale clipboard contents, or confusion between operator and provider IDs are realistic mistakes. The existing rate-limit guard (`try_reserve_capacity_for_node_provider_operation`) does not prevent the single-step transfer from succeeding. [4](#0-3) 

### Recommendation
Implement a two-step transfer for `node_provider_principal_id`:

1. **`propose_node_provider_transfer`** — callable by the current node provider; stores the proposed new `node_provider_id` as a pending value in the node operator record without applying it.
2. **`accept_node_provider_transfer`** — callable only by the proposed new `node_provider_id`; finalizes the transfer.

This mirrors the standard two-step ownership-transfer pattern and ensures the new principal can actually receive and accept the role before the old one is revoked.

### Proof of Concept
1. Node provider Alice controls a node operator record where `node_provider_principal_id = Alice`.
2. Alice calls `update_node_operator_config_directly` with `node_provider_id = <typo_principal>` (e.g., one character wrong in the textual encoding).
3. The registry immediately writes `node_provider_principal_id = <typo_principal>` to the record.
4. Alice's next call to `update_node_operator_config_directly` is rejected: `"Caller Alice not equal to the node_provider_principal_id for this record."` [1](#0-0) 
5. `<typo_principal>` (which Alice does not control) now owns the node operator record. Alice has permanently lost direct administrative access and must resort to an NNS governance proposal to recover.

### Citations

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L59-65)
```rust
        if caller
            != PrincipalId::try_from(&node_operator_record.node_provider_principal_id).unwrap()
        {
            return Err(format!(
                "Caller {caller} not equal to the node_provider_princpal_id for this record."
            ));
        }
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L67-70)
```rust
        // 3. Check Rate Limits
        let current_node_provider = caller;
        let reservation =
            self.try_reserve_capacity_for_node_provider_operation(now, current_node_provider, 1)?;
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs (L72-81)
```rust
        // 4. Check that the Node Provider is not being set with the same ID as the Node Operator
        let node_provider_id = payload
            .node_provider_id
            .ok_or("No Node Provider specified in the payload".to_string())?;

        if node_provider_id == node_operator_id {
            return Err(format!(
                "The Node Operator ID cannot be the same as the Node Provider ID: {node_operator_id}"
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
