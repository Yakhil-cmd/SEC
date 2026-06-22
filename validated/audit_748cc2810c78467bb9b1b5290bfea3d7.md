### Title
Node Provider Receives ICP Rewards for Removed Nodes Due to Non-Decremented `rewardable_nodes` Counter — (File: `rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs`)

---

### Summary

When a node operator removes their node(s) via `do_remove_node_directly()`, the `rewardable_nodes` counter in `NodeOperatorRecord` is never decremented. Because the monthly ICP reward calculation (`calculate_rewards_v0`) reads `rewardable_nodes` directly from the registry record, a node provider continues to receive ICP rewards for nodes they no longer operate.

---

### Finding Description

`NodeOperatorRecord` contains a `rewardable_nodes` field — a map from node type to count — that is the direct input to the reward calculation: [1](#0-0) 

`calculate_rewards_v0` iterates over this field to compute monthly XDR rewards per node provider: [2](#0-1) 

The field is set by governance via `do_update_node_operator_config`, which replaces it wholesale: [3](#0-2) 

When a node operator removes a node via `do_remove_node_directly()`, the mutation logic only increments `node_allowance` — it does **not** decrement `rewardable_nodes`: [4](#0-3) 

The same omission exists in the governance-triggered `do_remove_nodes()`: [5](#0-4) 

Neither removal path touches `rewardable_nodes`. After all physical nodes are deleted from the registry, the `NodeOperatorRecord` still carries the original `rewardable_nodes` map, and `calculate_rewards_v0` uses it to compute a full reward: [6](#0-5) 

The node rewards canister's legacy monthly-reward endpoint also calls this same path: [7](#0-6) 

---

### Impact Explanation

A node provider whose operator removes all physical nodes retains a non-zero `rewardable_nodes` entry. Every subsequent monthly reward cycle mints ICP to that provider as if the nodes were still running. The over-payment is proportional to the node count and type (e.g., `type3` nodes carry the highest per-node XDR rate). Because the NNS Governance canister calls `get_node_providers_monthly_xdr_rewards` to drive actual ICP minting, the inflated `rewardable_nodes` value directly causes excess ICP to be minted and transferred to the node provider's reward account — a ledger conservation violation. [8](#0-7) 

---

### Likelihood Explanation

`do_remove_node_directly` is callable by any registered node operator for their own nodes — no governance vote is required: [9](#0-8) 

A node operator who wishes to exit the network (decommission hardware, reduce costs) can call this endpoint for each of their nodes. Unless governance separately submits and executes an `UpdateNodeOperatorConfig` proposal to zero out `rewardable_nodes`, the stale counter persists indefinitely. There is no on-chain enforcement or invariant check that `rewardable_nodes ≤ actual node count`. The scenario is directly reachable by any registered node operator without any privileged access. [10](#0-9) 

---

### Recommendation

1. **Decrement `rewardable_nodes` on node removal**: In both `make_remove_or_replace_node_mutations` and `do_remove_nodes`, after fetching the `NodeOperatorRecord`, look up the removed node's `node_reward_type` and decrement the corresponding entry in `rewardable_nodes` (flooring at zero).

2. **Add a registry invariant**: Enforce that `sum(rewardable_nodes.values()) ≤ actual node count for that operator` as part of `maybe_apply_mutation_internal`'s invariant checks.

3. **Derive rewards from actual nodes**: The newer `get_rewardable_nodes_per_provider` in the node rewards canister already iterates actual `NodeRecord` entries rather than trusting `rewardable_nodes`. Migrating the monthly XDR reward path to this approach eliminates the stale-counter class of bugs entirely. [11](#0-10) 

---

### Proof of Concept

1. Governance executes `UpdateNodeOperatorConfig` setting `rewardable_nodes = {"type1": 10}` for operator `O` (normal onboarding).
2. Operator `O` calls `do_remove_node_directly` ten times, removing all ten physical nodes. Each call increments `node_allowance` but leaves `rewardable_nodes = {"type1": 10}` unchanged.
3. At the next monthly reward cycle, NNS Governance calls `get_node_providers_monthly_xdr_rewards`. `calculate_rewards_v0` reads `rewardable_nodes = {"type1": 10}` and computes a full reward for 10 type-1 nodes.
4. The CMC mints the computed ICP amount to the node provider's reward account, despite zero nodes being operated.
5. This repeats every month until governance manually zeroes out `rewardable_nodes` — an off-chain, non-enforced step. [12](#0-11) [4](#0-3)

### Citations

**File:** rs/protobuf/def/registry/node_operator/v1/node_operator.proto (L26-28)
```text
  // A map from node type to the number of nodes for which the associated Node
  // Provider should be rewarded.
  map<string, uint32> rewardable_nodes = 5;
```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L17-61)
```rust
pub fn calculate_rewards_v0(
    rewards_table: &NodeRewardsTable,
    node_operators: &[(String, NodeOperatorRecord)],
    data_centers: &BTreeMap<String, DataCenterRecord>,
) -> Result<RewardsPerNodeProvider, String> {
    // The reward coefficients for the NP, at the moment used only for type3 nodes, as a measure for stimulating decentralization.
    // It is kept outside of the reward calculation loop in order to reduce node rewards for NPs with multiple DCs.
    // We want to have as many independent NPs as possible for the given reward budget.
    let mut np_coefficients: HashMap<String, f64> = HashMap::new();

    let mut rewards = BTreeMap::new();
    let mut computation_log = BTreeMap::new();

    for (key_string, node_operator) in node_operators.iter() {
        let node_operator_id = PrincipalId::try_from(&node_operator.node_operator_principal_id)
            .map_err(|e| {
                format!(
                    "Node Operator key '{key_string:?}' cannot be parsed as a PrincipalId: '{e}'"
                )
            })?;

        let node_provider_id = PrincipalId::try_from(&node_operator.node_provider_principal_id)
            .map_err(|e| {
                format!(
                    "Node Operator with key '{node_operator_id}' has a node_provider_principal_id \
                                 that cannot be parsed as a PrincipalId: '{e}'"
                )
            })?;

        let dc = data_centers.get(&node_operator.dc_id).ok_or_else(|| {
            format!(
                "Node Operator with key '{}' has data center ID '{}' \
                            not found in the Registry",
                node_operator_id, node_operator.dc_id
            )
        })?;
        let region = &dc.region;

        let np_rewards = rewards.entry(node_provider_id).or_default();
        let np_log = computation_log
            .entry(node_provider_id)
            .or_insert(RewardsPerNodeProviderLog::new(node_provider_id));

        for (node_type, node_count) in node_operator.rewardable_nodes.iter() {
            let rate = match rewards_table.get_rate(region, node_type) {
```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L127-143)
```rust
                    for i in 0..*node_count {
                        let node_reward = (reward_base * np_coeff) as u64;
                        np_log.add_entry(LogEntry::NodeRewards {
                            node_type: node_type.clone(),
                            node_idx: i,
                            dc_id: node_operator.dc_id.clone(),
                            rewardable_count: *node_count,
                            rewards_xdr_permyriad: node_reward,
                        });
                        dc_reward += node_reward;
                        np_coeff *= dc_reward_coefficient_percent;
                    }
                    np_coefficients.insert(np_coefficients_key, np_coeff);
                    dc_reward
                }
                _ => *node_count as u64 * rate.xdr_permyriad_per_node_per_month,
            };
```

**File:** rs/registry/canister/src/mutations/do_update_node_operator_config.rs (L47-49)
```rust
        if !payload.rewardable_nodes.is_empty() {
            node_operator_record.rewardable_nodes = payload.rewardable_nodes;
        }
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L18-29)
```rust
impl Registry {
    /// Removes an existing node from the registry.
    ///
    /// This method is called directly by the node operator tied to the node.
    pub fn do_remove_node_directly(&mut self, payload: RemoveNodeDirectlyPayload) {
        let caller_id = dfn_core::api::caller();
        println!("{LOG_PREFIX}do_remove_node_directly started: {payload:?} caller: {caller_id:?}");
        self.do_remove_node_directly_(payload.clone(), caller_id, now_system_time())
            .unwrap();

        println!("{LOG_PREFIX}do_remove_node_directly finished: {payload:?}");
    }
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L83-119)
```rust
        // 2. Compare the caller_id (node operator) with the node's node operator and, if that fails,
        // fall back to comparing the DC and the node provider ID for the caller and the node.
        // That covers the case when the node provider added a new operator record in the same DC, and
        // is trying to redeploy the nodes under the new operator.
        // Hence, if the DC and the node provider of the caller and the original node operator match,
        // the removal should succeed.
        if caller_id != node_operator_id {
            let node_operator_caller = get_node_operator_record(self, caller_id)
                .map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                })
                .unwrap();
            let dc_caller = node_operator_caller.dc_id;
            let dc_orig_node_operator = get_node_operator_record(self, node_operator_id)
                .map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                })
                .unwrap()
                .dc_id;
            assert_eq!(
                dc_caller, dc_orig_node_operator,
                "The DC {dc_caller} of the caller {caller_id}, does not match the DC of the node {dc_orig_node_operator}."
            );
            let node_provider_caller = get_node_provider_id_for_operator_id(self, caller_id)
                .map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                });
            let node_provider_of_the_node =
                get_node_provider_id_for_operator_id(self, node_operator_id).map_err(|e| {
                    format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {e}")
                });
            assert_eq!(
                node_provider_caller, node_provider_of_the_node,
                "The node provider {:?} of the caller {}, does not match the node provider {:?} of the node {}.",
                node_provider_caller, caller_id, node_provider_of_the_node, payload.node_id
            );
        }
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_node_directly.rs (L185-204)
```rust
        // 5. Retrieve the NO record and increment its node allowance by 1
        let mut updated_node_operator_record = get_node_operator_record(self, node_operator_id)
            .map_err(|err| {
                format!("{LOG_PREFIX}do_remove_node_directly: Aborting node removal: {err}")
            })
            .unwrap();
        updated_node_operator_record.node_allowance += 1;

        // 6. Finally, generate the following mutations:
        //   * Delete the node record
        //   * Delete entries for node encryption keys
        //   * Increment NO's allowance by 1
        mutations.extend(make_remove_node_registry_mutations(self, payload.node_id));
        // mutation to update node operator value
        mutations.push(make_update_node_operator_mutation(
            node_operator_id,
            &updated_node_operator_record,
        ));

        mutations
```

**File:** rs/registry/canister/src/mutations/node_management/do_remove_nodes.rs (L57-79)
```rust
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

        // 8. Create node operator update mutations
        for (node_operator_id, updated_node_operator) in principal_to_node_operator_record {
            mutations.push(make_update_node_operator_mutation(
                node_operator_id,
                &updated_node_operator,
            ));
```

**File:** rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs (L35-59)
```rust
        let node_operators = get_key_family_iter_at_version::<NodeOperatorRecord>(
            self,
            NODE_OPERATOR_RECORD_KEY_PREFIX,
            version,
        )
        .collect::<Vec<_>>();

        let data_centers = get_key_family_iter_at_version::<DataCenterRecord>(
            self,
            DATA_CENTER_KEY_PREFIX,
            version,
        )
        .collect::<BTreeMap<String, DataCenterRecord>>();

        let reward_values = calculate_rewards_v0(&rewards_table, &node_operators, &data_centers)?;

        rewards.rewards = reward_values
            .rewards_per_node_provider
            .into_iter()
            .map(|(k, v)| (k.to_string(), v))
            .collect();

        rewards.registry_version = Some(version);

        Ok(rewards)
```

**File:** rs/node_rewards/canister/src/canister/mod.rs (L295-311)
```rust
            let node_operators = decoded_key_value_pairs_for_prefix::<NodeOperatorRecord>(
                &*registry_client,
                NODE_OPERATOR_RECORD_KEY_PREFIX,
                version,
            )?;

            let data_centers = decoded_key_value_pairs_for_prefix::<DataCenterRecord>(
                &*registry_client,
                DATA_CENTER_KEY_PREFIX,
                version,
            )?
            .into_iter()
            .collect();

            calculate_rewards_v0(&rewards_table, &node_operators, &data_centers)
                .map(|rewards| (rewards, version))
        }
```

**File:** rs/node_rewards/canister/src/registry_querier.rs (L155-226)
```rust
    pub fn get_rewardable_nodes_per_provider(
        &self,
        date: &NaiveDate,
        provider_filter: Option<&PrincipalId>,
    ) -> Result<BTreeMap<PrincipalId, Vec<RewardableNode>>, RegistryClientError> {
        let mut rewardable_nodes_per_provider: BTreeMap<_, Vec<RewardableNode>> = BTreeMap::new();
        let registry_version = self
            .version_for_timestamp_nanoseconds(last_unix_timestamp_nanoseconds(date))
            .unwrap();

        // Bulk-fetch all data upfront instead of per-node individual lookups.
        let nodes = self.nodes_in_version(registry_version)?;
        let all_operators = self.all_node_operators(registry_version)?;
        let all_data_centers = self.all_data_centers(registry_version)?;

        for (node_id, node_record) in nodes {
            let node_operator_id: PrincipalId = node_record
                .node_operator_id
                .try_into()
                .expect("Failed to parse PrincipalId from node operator ID");

            let Some(node_operator_record) = all_operators.get(&node_operator_id) else {
                ic_cdk::println!("Node {} has no NodeOperatorRecord: skipping", node_id);
                continue;
            };

            let Some(data_center_record) = all_data_centers.get(&node_operator_record.dc_id) else {
                ic_cdk::println!(
                    "Node {} has NodeOperator but no DataCenterRecord for dc_id {}: skipping",
                    node_id,
                    node_operator_record.dc_id
                );
                continue;
            };

            let node_provider_id: PrincipalId = node_operator_record
                .node_provider_principal_id
                .clone()
                .try_into()
                .expect("Failed to parse PrincipalId");

            if let Some(provider_filter) = provider_filter
                && &node_provider_id != provider_filter
            {
                continue;
            }
            let Some(some_reward_type) = node_record.node_reward_type else {
                // If the node does not have a node_reward_type, we skip it.
                continue;
            };

            let node_reward_type =
                NodeRewardType::try_from(some_reward_type).expect("Invalid node_reward_type value");

            // type4.5 nodes are rewarded as type1.1 nodes.
            let node_reward_type = if node_reward_type == NodeRewardType::Type4dot5 {
                NodeRewardType::Type1dot1
            } else {
                node_reward_type
            };

            rewardable_nodes_per_provider
                .entry(node_provider_id)
                .or_default()
                .push(RewardableNode {
                    node_id,
                    node_reward_type,
                    dc_id: node_operator_record.dc_id.clone(),
                    region: data_center_record.region.clone(),
                });
        }
        Ok(rewardable_nodes_per_provider)
```
