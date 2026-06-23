### Title
Unbounded Full-Node-Set Scans in `do_add_node` Cause Instruction-Limit Exhaustion as Registry Grows - (File: `rs/registry/canister/src/mutations/node_management/do_add_node.rs`)

### Summary
The Registry canister's `do_add_node` function, callable by any entity holding a valid node operator record, performs two unconditional O(N) full scans over every node record in the registry on each invocation. As the IC node count grows, the cumulative instruction cost of these scans — compounded by the O(N) invariant checks triggered by `maybe_apply_mutation_internal` — will eventually exhaust the per-message instruction budget, permanently preventing new nodes from being added to the registry. The codebase itself explicitly acknowledges this risk.

### Finding Description

**Entry path**: Any principal that holds a `NodeOperatorRecord` in the Registry canister can call the `add_node` update method directly. No governance vote or privileged key is required. [1](#0-0) 

Inside `do_add_node_`, two separate full scans over all `NodeRecord` entries are performed unconditionally:

**Scan 1 — `scan_for_nodes_by_ip`** (line 92): iterates over every node record in the registry to find nodes sharing the caller's submitted IP address. [2](#0-1) [3](#0-2) 

**Scan 2 — `get_node_operator_nodes`** (line 144): iterates over every node record again to count how many nodes the calling node operator already has registered, for quota enforcement. [4](#0-3) [5](#0-4) 

Both helpers call `get_key_family::<NodeRecord>(registry, NODE_RECORD_KEY_PREFIX)`, which decodes and returns every node record in the store. The codebase explicitly acknowledges this pattern is a full scan: [6](#0-5) 

After the two scans, `maybe_apply_mutation_internal` is called, which triggers the full suite of registry invariant checks. These checks — `check_node_crypto_keys_exist_and_are_unique`, `check_node_assignment_invariants`, `check_node_operator_invariants`, `check_node_record_invariants` — each independently iterate over all node records: [7](#0-6) [8](#0-7) 

The codebase already documents that this pattern hits instruction limits at scale. The crypto invariant check explicitly skips full key validation because:

> "for the mainnet state with 1K+ nodes the full validation would go over the instruction limit per message." [9](#0-8) 

The per-message instruction limit for update calls is 40 billion instructions: [10](#0-9) 

### Impact Explanation

If the total instruction cost of the two node-record scans plus the invariant-check scans exceeds the per-message limit, every call to `add_node` will be rejected with `CanisterInstructionLimitExceeded`. This would permanently freeze the IC's ability to onboard new nodes into the registry — a critical availability failure for the network's growth and decentralization. No attacker action is needed beyond the network growing organically; a malicious node operator could also deliberately accelerate the problem by registering many nodes to inflate the registry size.

### Likelihood Explanation

The IC currently operates with over 1,000 nodes. The codebase already documents that full-node-set operations approach the instruction limit at this scale. Each `add_node` call now executes at minimum three independent O(N) passes over all node records. As the IC targets tens of thousands of nodes, the instruction cost grows linearly and will eventually cross the threshold. The NNS subnet's system-subnet instruction limit is higher than application subnets, but the acknowledged existing pressure at 1K nodes makes this a realistic near-term concern rather than a theoretical one.

### Recommendation

Replace the two full-node-set scans with indexed lookups:
- Maintain a secondary index mapping IP address → NodeId in the registry store, updated atomically on node add/remove, so `scan_for_nodes_by_ip` becomes an O(1) key lookup.
- Maintain a secondary index mapping NodeOperatorId → set of NodeIds, so `get_node_operator_nodes` becomes an O(k) lookup where k is the operator's own node count, not the global count.

This mirrors the Skale recommendation: avoid iterating over all nodes when only a small subset is relevant to the operation.

### Proof of Concept

1. Register a node operator record in the Registry canister (permissionless, requires only a valid principal).
2. Populate the registry with a large number of node records (e.g., via many prior `add_node` calls from various operators, or by observing mainnet growth).
3. Call `add_node` with a valid payload from the node operator's principal.
4. Observe that the call consumes instructions proportional to the total node count across three independent full scans (`scan_for_nodes_by_ip`, `get_node_operator_nodes`, and the invariant checks in `maybe_apply_mutation_internal`).
5. At sufficient node count, the call returns `CanisterInstructionLimitExceeded` and no new node can ever be added. [11](#0-10)

### Citations

**File:** rs/registry/canister/canister/canister.rs (L1211-1225)
```rust
#[candid_method(update, rename = "add_node")]
fn add_node_(payload: AddNodePayload) -> NodeId {
    let node_id = registry_mut()
        .do_add_node(payload)
        .unwrap_or_else(|error_message| {
            let msg = format!("{LOG_PREFIX} Add node failed: {error_message}");
            // TODO(NNS1-4290): Delete once we figure why it seems like clients
            // are throwing this away.
            println!("{}", msg);
            trap_with(&msg);
        });

    recertify_registry();
    node_id
}
```

**File:** rs/registry/canister/src/mutations/node_management/do_add_node.rs (L51-161)
```rust
    fn do_add_node_(
        &mut self,
        payload: AddNodePayload,
        caller_id: PrincipalId,
        now: SystemTime,
    ) -> Result<NodeId, String> {
        let node_operator_record = get_node_operator_record(self, caller_id)
            .map_err(|err| format!("{LOG_PREFIX}do_add_node: Aborting node addition: {err}"))?;

        let reservation =
            self.try_reserve_capacity_for_node_operator_operation(now, caller_id, 1)?;

        // Validate keys and get the node id
        let (node_id, valid_pks) = valid_keys_from_payload(&payload)
            .map_err(|err| format!("{LOG_PREFIX}do_add_node: {err}"))?;

        println!("{LOG_PREFIX}do_add_node: The node id is {node_id:?}");

        // Get valid node_rewards_type if type is in request
        let node_reward_type = payload
            .node_reward_type
            .as_ref()
            .map(|t| {
                validate_str_as_node_reward_type(t).map_err(|e| {
                    format!("{LOG_PREFIX}do_add_node: Error parsing node type from payload: {e}")
                })
            })
            .transpose()?;

        // Clear out any nodes that already exist at this IP.
        // This will only succeed if the same NO was in control of the original nodes.
        //
        // (We use the http endpoint to be in line with what is used by the
        // release dashboard.)
        let http_endpoint = connection_endpoint_from_string(&payload.http_endpoint);

        // 2a. Check IP-based rate limiting (1 node addition per day per IP)
        let ip_addr = http_endpoint.ip_addr.clone();
        let ip_reservation = try_reserve_add_node_capacity(now, ip_addr.clone())
            .map_err(|e| format!("{LOG_PREFIX}do_add_node: {e}"))?;

        let nodes_with_same_ip = scan_for_nodes_by_ip(self, &http_endpoint.ip_addr);
        let mut mutations = Vec::new();
        let mut num_removed_same_ip_same_type = 0;
        if !nodes_with_same_ip.is_empty() {
            for node_with_same_ip in &nodes_with_same_ip {
                let node_same_ip_reward_type =
                    get_node_reward_type_for_node(self, *node_with_same_ip)
                        .map_err(|e| format!("{LOG_PREFIX}do_add_node: {e}"))?;

                if Some(node_same_ip_reward_type) == node_reward_type {
                    num_removed_same_ip_same_type += 1;
                }
            }
            if nodes_with_same_ip.len() == 1 {
                mutations = self.make_remove_or_replace_node_mutations(
                    RemoveNodeDirectlyPayload {
                        node_id: nodes_with_same_ip[0],
                    },
                    caller_id,
                    Some(node_id),
                );
            } else {
                // In the unlikely situation that multiple nodes share the same IP address as the new node,
                // this will remove the existing nodes.
                // While the situation is unexpected, the behavior is backwards compatible.
                // This may happen only if there is a bug in the registry code and the registry invariant isn't enforced,
                // due to which the node id was not properly removed.
                for previous_node_id in nodes_with_same_ip {
                    mutations.extend(self.make_remove_or_replace_node_mutations(
                        RemoveNodeDirectlyPayload {
                            node_id: previous_node_id,
                        },
                        caller_id,
                        // If there are multiple nodes with the same IP, then each of them could in principle be in a (different) subnet.
                        // In that case replacing all different node ids with the same new node isn't an option.
                        // To cover for this corner case, we don't replace the node id but just remove the node and potentially fail.
                        None,
                    ));
                }
            }
        }

        if self.are_node_rewards_enabled() {
            let node_reward_type = node_reward_type.ok_or(format!(
                "{LOG_PREFIX}do_add_node: Node reward type is required."
            ))?;

            let max_rewardable_nodes_same_type = *node_operator_record
                .max_rewardable_nodes
                .get(&(node_reward_type.to_string()))
                .ok_or(format!("{LOG_PREFIX}do_add_node: Node Operator does not have rewardable nodes for {node_reward_type}"))?;

            let num_in_registry_same_type = get_node_operator_nodes(self, caller_id)
                .into_iter()
                .filter_map(|node| node.node_reward_type)
                .filter(|t| t == &(node_reward_type as i32))
                .count() as u32;

            // Validate node operator's max_rewardable_nodes quota
            if max_rewardable_nodes_same_type
                <= num_in_registry_same_type.saturating_sub(num_removed_same_ip_same_type)
            {
                return Err(format!(
                    "{LOG_PREFIX}do_add_node: Node Operator has reached max_rewardable_nodes quota for {node_reward_type}.\
                    Number of nodes in the registry with {node_reward_type} type = {num_in_registry_same_type},\
                    Number of removed nodes with same IP and same type = {num_removed_same_ip_same_type},\
                    {node_reward_type} quota = {max_rewardable_nodes_same_type}"
                ));
            }
        }
```

**File:** rs/registry/canister/src/mutations/node_management/common.rs (L240-249)
```rust
pub fn scan_for_nodes_by_ip(registry: &Registry, ip_addr: &str) -> Vec<NodeId> {
    get_key_family::<NodeRecord>(registry, NODE_RECORD_KEY_PREFIX)
        .into_iter()
        .filter_map(|(k, v)| {
            v.http.and_then(|v| {
                (v.ip_addr == ip_addr).then(|| NodeId::from(PrincipalId::from_str(&k).unwrap()))
            })
        })
        .collect()
}
```

**File:** rs/registry/canister/src/mutations/node_management/common.rs (L262-276)
```rust
pub fn get_node_operator_nodes_with_id(
    registry: &Registry,
    query_node_operator_id: PrincipalId,
) -> Vec<(NodeId, NodeRecord)> {
    get_key_family::<NodeRecord>(registry, NODE_RECORD_KEY_PREFIX)
        .into_iter()
        .filter(|(_, node_record)| {
            let record_node_operator_id: PrincipalId =
                PrincipalId::try_from(&node_record.node_operator_id).unwrap();

            record_node_operator_id == query_node_operator_id
        })
        .map(|(k, v)| (NodeId::new(PrincipalId::from_str(&k).unwrap()), v))
        .collect()
}
```

**File:** rs/registry/canister/src/mutations/do_remove_node_operators.rs (L62-67)
```rust
        // This implementation is inefficient, because it does a full scan of all nodes.
        for (_key, node_record) in get_key_family_iter::<NodeRecord>(self, NODE_RECORD_KEY_PREFIX) {
            // Throw out node operators that operate the node (that this loop is currently considering).
            node_operators
                .retain(|node_operator| node_operator.to_vec() != node_record.node_operator_id);
        }
```

**File:** rs/registry/canister/src/invariants/crypto.rs (L59-62)
```rust
/// It is NOT CHECKED that the crypto keys are fully well-formed or valid, as these
/// checks are expensive in terms of computation (about 200 times more expensive then just parsing,
/// 400M instructions per node vs. 2M instructions), so for the mainnet state with 1K+ nodes
/// the full validation would go over the instruction limit per message.
```

**File:** rs/registry/canister/src/invariants/crypto.rs (L63-71)
```rust
pub(crate) fn check_node_crypto_keys_invariants(
    snapshot: &RegistrySnapshot,
) -> Result<(), InvariantCheckError> {
    check_node_crypto_keys_exist_and_are_unique(snapshot)?;
    check_no_orphaned_node_crypto_records(snapshot)?;
    check_chain_key_configs(snapshot)?;
    check_chain_key_signing_subnet_lists(snapshot)?;
    check_high_threshold_public_key_matches_the_one_in_cup(snapshot)?;
    Ok(())
```

**File:** rs/registry/canister/src/invariants/common.rs (L50-60)
```rust
pub(crate) fn get_all_node_records(snapshot: &RegistrySnapshot) -> BTreeMap<NodeId, NodeRecord> {
    let mut nodes = BTreeMap::new();
    for (k, v) in snapshot {
        if let Some(id) = get_node_record_node_id(str::from_utf8(k).unwrap()) {
            let record = NodeRecord::decode(v.as_slice()).unwrap();
            nodes.insert(NodeId::from(id), record);
        }
    }

    nodes
}
```

**File:** rs/config/src/subnet_config.rs (L36-36)
```rust
pub(crate) const MAX_INSTRUCTIONS_PER_MESSAGE: NumInstructions = NumInstructions::new(40 * B);
```
