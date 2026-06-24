### Title
`do_remove_nodes` Deletes All Crypto Keys of an API Boundary Node Without Removing Its Registry Record, Causing Invariant Violation and Subnet Stall - (File: `rs/registry/canister/src/mutations/node_management/do_remove_nodes.rs`)

### Summary

The governance-triggered `do_remove_nodes` function removes a node's record and all its crypto keys from the registry without checking whether the node is also an active API boundary node. This leaves an orphaned `ApiBoundaryNodeRecord` in the registry pointing to a node with no corresponding `NodeRecord`. The `check_api_boundary_node_invariants` function explicitly enforces that every `ApiBoundaryNodeRecord` must have a corresponding `NodeRecord`, and the registry's own comment warns that violating this causes a **persistent transient error in `message_routing.rs` that stalls the subnet**.

### Finding Description

`do_remove_nodes` is the governance-canister-callable path for removing nodes from the registry. It performs two checks before deletion:

1. Skip nodes not found in the registry (step 4).
2. Panic if the node is a member of a subnet (step 5).

It does **not** check whether the node is an API boundary node. [1](#0-0) 

After passing those checks, it calls `make_remove_node_registry_mutations`, which deletes the node record **and all five crypto key entries** (CommitteeSigning, NodeSigning, DkgDealingEncryption, TLS certificate, IDkgMEGaEncryption) plus the firewall ruleset: [2](#0-1) 

The `ApiBoundaryNodeRecord` keyed by `make_api_boundary_node_record_key(node_id)` is **never deleted** by this path.

By contrast, `do_remove_node_directly` — the node-operator-callable path — explicitly panics when the target node is an active API boundary node and no replacement is provided: [3](#0-2) 

After `do_remove_nodes` commits its mutations, `check_global_state_invariants` runs `check_api_boundary_node_invariants`: [4](#0-3) 

That invariant iterates every `ApiBoundaryNodeRecord` and requires a matching `NodeRecord`: [5](#0-4) 

Because `do_remove_nodes` deletes the `NodeRecord` but leaves the `ApiBoundaryNodeRecord`, the post-mutation snapshot will contain an `ApiBoundaryNodeRecord` with no corresponding `NodeRecord`. The invariant check will therefore **panic**, rolling back the entire mutation batch and leaving the registry in its pre-mutation state — meaning the node record and all its crypto keys are **not** deleted, but the governance proposal is consumed and cannot be retried without a new proposal.

The registry comment at line 26–29 of `api_boundary_node.rs` explicitly warns about the consequence of this invariant being violated at the `message_routing.rs` level: [6](#0-5) 

> "An attempt to read the related NodeRecord for an API BN would fail and cause `ReadRegistryError::Transient()` — Transient registry errors are retried in `message_route.rs` code. However, in this case it's not helpful, the error is persistent in nature — As a result, the subnet is stalled."

### Impact Explanation

A governance proposal to remove a node that is simultaneously an active API boundary node will panic inside `check_global_state_invariants` after the mutations are prepared. The panic rolls back the registry write, so the node is **not** removed. The governance proposal is consumed. The node operator cannot remove the node through the governance path without first separately removing the API boundary node record via `remove_api_boundary_nodes`. If the API boundary node record is somehow committed in a state without a `NodeRecord` (e.g., through a sequencing race or a future code path), `message_routing` on every subnet node will enter a persistent retry loop on a transient error, **stalling the subnet**.

### Likelihood Explanation

Any NNS governance participant can submit a `RemoveNodes` proposal targeting a node that is also an API boundary node. API boundary nodes are unassigned (not in any subnet), so the subnet-membership guard in `do_remove_nodes` does not fire. The node operator may legitimately want to decommission a node that serves both roles, and the governance proposal path is the standard mechanism for doing so. The missing guard is not documented, so proposers have no indication the action will fail or produce inconsistent state.

### Recommendation

Add an API boundary node check in `do_remove_nodes` analogous to the one in `do_remove_node_directly`. Either:

1. **Panic** if any node in the payload is an active API boundary node, requiring the caller to first remove the API boundary node record via `remove_api_boundary_nodes`; or
2. **Automatically include** a `delete(make_api_boundary_node_record_key(node_id))` mutation for any node that is an API boundary node, mirroring the cleanup already performed for subnet membership and crypto keys.

### Proof of Concept

1. Register node `N` with a domain name so it qualifies as an API boundary node.
2. Call `do_add_api_boundary_nodes` (governance) to insert an `ApiBoundaryNodeRecord` for `N`. Node `N` is now unassigned (not in any subnet) and is an API boundary node.
3. Submit a governance `RemoveNodes` proposal with `node_ids = [N]`.
4. The proposal passes; `do_remove_nodes` is invoked.
5. Step 5 of `do_remove_nodes` checks subnet membership — `N` is not in any subnet, so the check passes.
6. `make_remove_node_registry_mutations` generates deletions for the `NodeRecord` and all five crypto keys of `N`. The `ApiBoundaryNodeRecord` is not included.
7. `maybe_apply_mutation_internal` calls `check_global_state_invariants`, which calls `check_api_boundary_node_invariants`.
8. The invariant finds `ApiBoundaryNodeRecord` for `N` but no `NodeRecord` for `N` → returns `Err` → `check_global_state_invariants` panics.
9. The registry mutation is rolled back. The governance proposal is consumed. Node `N` remains in the registry with its crypto keys intact but the governance action is lost. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/registry/canister/src/mutations/node_management/do_remove_nodes.rs (L36-72)
```rust
        let mut mutations : Vec<_> = nodes_to_be_removed
            .into_iter().flat_map(|node_to_remove| {
                // 4. Skip nodes that are not in the registry.
                // This tackles the race condition where a node is removed from the registry
                // by another transaction before this transaction is processed.
                let Some(node_record) = self.get_node(node_to_remove) else {
                        println!("{LOG_PREFIX}do_remove_nodes: node {node_to_remove} not found in registry, skipping");
                        return vec![];
                };

                let node_operator_id = PrincipalId(Principal::from_slice(&node_record.node_operator_id));

                // 5. Ensure node is not in a subnet
                let is_node_in_subnet = find_subnet_for_node(self, node_to_remove, &subnet_list_record);
                if let Some(subnet_id) = is_node_in_subnet {
                    panic!("{}do_remove_nodes: Cannot remove a node that is a member of a subnet. This node is a member of Subnet: {}",
                        LOG_PREFIX,
                        make_subnet_record_key(subnet_id)
                    );
                }

                // 6. Retrieve the NO record, cache it and increment its node allowance by 1
                let new_node_operator_record = principal_to_node_operator_record.entry(node_operator_id).or_insert_with(|| get_node_operator_record(self, node_operator_id)
                    .map_err(|err| {
                        format!(
                            "{LOG_PREFIX}do_remove_nodes: Aborting node removal: {err}"
                        )
                    })
                    .unwrap());
                new_node_operator_record.node_allowance += 1;


                // 7. Finally, generate the following mutations:
                //   * Delete the node
                //   * Delete entries for node encryption keys
                make_remove_node_registry_mutations(self, node_to_remove)
        }).collect();
```

**File:** rs/registry/canister/src/mutations/node_management/common.rs (L201-237)
```rust
pub fn make_remove_node_registry_mutations(
    registry: &Registry,
    node_id: NodeId,
) -> Vec<RegistryMutation> {
    let node_key = make_node_record_key(node_id);
    let committee_signing_key = make_crypto_node_key(node_id, KeyPurpose::CommitteeSigning);
    let node_signing_key = make_crypto_node_key(node_id, KeyPurpose::NodeSigning);
    let dkg_dealing_key = make_crypto_node_key(node_id, KeyPurpose::DkgDealingEncryption);
    let tls_cert_key = make_crypto_tls_cert_key(node_id);
    let idkg_dealing_key = make_crypto_node_key(node_id, KeyPurpose::IDkgMEGaEncryption);
    let firewall_ruleset_key = make_firewall_rules_record_key(&FirewallRulesScope::Node(node_id));

    let keys_to_maybe_remove = [
        node_key,
        committee_signing_key,
        node_signing_key,
        dkg_dealing_key,
        tls_cert_key,
        idkg_dealing_key,
        firewall_ruleset_key,
    ];

    let latest_version = registry.latest_version();

    keys_to_maybe_remove
        .iter()
        .flat_map(|key| {
            // It is possible, for example, that IDkgMEGaEncryption key is not present
            // or that other keys are not present.  When we have enabled the invariants
            // for the keys being all present for each node_id and removed with the node_id,
            // we can simply return the list of mutations without filtering
            registry
                .get(key.as_bytes(), latest_version)
                .map(|_| delete(key))
        })
        .collect::<Vec<_>>()
}
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L123-142)
```rust
        // 3. Check if the node is an API Boundary Node. If there is a replacement node, remove the existing node
        //    and try to assign the new one to act as API boundary node. This will only work if the new node meets all
        //    the requirements of an API boundary node (e.g., it is configured with a domain name).
        if let Some(api_bn_record) = self.get_api_boundary_node_record(payload.node_id) {
            let Some(replacement_node_id) = new_node_id else {
                panic!(
                    "{}do_remove_node_directly: Cannot remove this node, as it is an active API boundary node: {}",
                    LOG_PREFIX,
                    make_api_boundary_node_record_key(payload.node_id)
                );
            };

            // remove the existing API boundary node record
            let old_key = make_api_boundary_node_record_key(payload.node_id);
            mutations.push(delete(old_key));

            // create the new API boundary node record by just cloning the old one and inserting it with the new key
            let new_key = make_api_boundary_node_record_key(replacement_node_id);
            mutations.push(insert(new_key, api_bn_record.clone().encode_to_vec()));
        }
```

**File:** rs/registry/canister/src/invariants/checks.rs (L102-103)
```rust
        // API Boundary Node invariant
        result = result.and(check_api_boundary_node_invariants(&snapshot));
```

**File:** rs/registry/canister/src/invariants/api_boundary_node.rs (L18-41)
```rust
pub(crate) fn check_api_boundary_node_invariants(
    snapshot: &RegistrySnapshot,
) -> Result<(), InvariantCheckError> {
    let mut domain_to_id: HashMap<String, NodeId> = HashMap::new();
    // IMPORTANT: this code structure below rigorously follows the structure of the `fn try_to_populate_api_boundary_nodes(..)` in message_routing.rs.
    // These two code blocks should be kept in sync to avoid stalling the subnets.
    // Please be very mindful when modifying the code below.
    // Here is an example of code changes leading to subnet stalling:
    // - Assume the requirement for an API BN to have a related NodeRecord is remove/relaxed below
    // - However, this requirement still exists and holds in the message_route.rs code
    // - An attempt to read the related NodeRecord for an API BN would fail and cause ReadRegistryError::Transient()
    // - Transient registry errors are retried in `message_route.rs` code. However, in this case it's not helpful, the error is persistent in nature
    // - As a result, the subnet is stalled
    let api_boundary_node_ids = get_api_boundary_node_ids_from_snapshot(snapshot)?;
    for api_bn_id in api_boundary_node_ids {
        let node_record = get_node_record_from_snapshot(api_bn_id, snapshot)?;
        let Some(node_record) = node_record else {
            return Err(InvariantCheckError {
                msg: format!(
                    "API Boundary Node with id={api_bn_id} doesn't have a corresponding NodeRecord"
                ),
                source: None,
            });
        };
```
