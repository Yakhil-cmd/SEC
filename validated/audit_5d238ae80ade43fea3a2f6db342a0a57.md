### Title
Node Provider Reward Inflation via Inflated `rewardable_nodes` in `NodeOperatorRecord` Without On-Chain Verification of Actual Running Nodes - (File: rs/registry/node_provider_rewards/src/lib.rs)

### Summary
The legacy (v0) node provider reward calculation in the Internet Computer reads the `rewardable_nodes` field directly from `NodeOperatorRecord` in the registry and pays rewards proportional to the declared node count, without verifying that those nodes are actually running, assigned to a subnet, or producing blocks. A node operator whose `rewardable_nodes` map is set to an inflated count — whether through a governance proposal or a registry mutation — will receive proportionally inflated ICP rewards every month, with no on-chain proof-of-work check in the reward path.

### Finding Description
The function `calculate_rewards_v0` in `rs/registry/node_provider_rewards/src/lib.rs` iterates over all `NodeOperatorRecord` entries and for each entry reads `node_operator.rewardable_nodes` — a plain integer map from node type to count — and multiplies it by the per-node XDR rate from the rewards table:

```rust
// rs/registry/node_provider_rewards/src/lib.rs line 60-142
for (node_type, node_count) in node_operator.rewardable_nodes.iter() {
    ...
    _ => *node_count as u64 * rate.xdr_permyriad_per_node_per_month,
```

There is no step that cross-references `rewardable_nodes` against the actual `NodeRecord` entries in the registry to confirm that the declared number of nodes of each type actually exist and are healthy. The `rewardable_nodes` field is a governance-settable integer in the `NodeOperatorRecord` protobuf:

```proto
// rs/protobuf/def/registry/node_operator/v1/node_operator.proto line 27-28
// A map from node type to the number of nodes for which the associated Node
// Provider should be rewarded.
map<string, uint32> rewardable_nodes = 5;
```

This field can be set at node-operator creation time (`do_add_node_operator`) and updated later via `do_update_node_operator_config`, both of which are governance-gated mutations. The `do_update_node_operator_config` handler unconditionally overwrites `rewardable_nodes` with whatever value is in the proposal payload:

```rust
// rs/registry/canister/src/mutations/do_update_node_operator_config.rs line 47-49
if !payload.rewardable_nodes.is_empty() {
    node_operator_record.rewardable_nodes = payload.rewardable_nodes;
}
```

No check is performed to ensure the new `rewardable_nodes` value is consistent with the number of `NodeRecord` entries that actually reference this operator. The monthly reward pipeline then calls `get_monthly_node_provider_rewards` → `get_node_providers_monthly_xdr_rewards` (registry canister) → `calculate_rewards_v0`, which trusts the declared count entirely.

The performance-based path (`get_node_providers_rewards` / `RegistryQuerier::get_rewardable_nodes_per_provider`) does iterate over actual `NodeRecord` entries and is therefore not affected. However, the legacy v0 path remains active when `are_performance_based_rewards_enabled()` returns false, and the governance canister selects between the two paths at runtime.

### Impact Explanation
A node operator whose `rewardable_nodes` is set to N (via a governance proposal) but who only operates M < N actual nodes will receive rewards for N nodes every month. Because the ICP minted is proportional to the declared count, the over-declaration directly inflates the ICP supply paid to that provider. At scale (e.g., declaring 1,000 nodes of type3 in a high-reward region while running 0), the monthly reward could be on the order of millions of XDR worth of ICP minted to the attacker's account. This is a **ledger conservation / cycles-resource accounting bug** with direct financial impact: ICP is minted without corresponding infrastructure being provided.

### Likelihood Explanation
Exploiting this requires submitting a governance proposal to set an inflated `rewardable_nodes` value for a node operator controlled by the attacker. Governance proposals require a neuron with sufficient voting power to pass, which is a significant barrier. However, the NNS governance system has historically passed node-operator configuration proposals with limited scrutiny of the exact `rewardable_nodes` values, since the field is not prominently surfaced in proposal reviews. A node provider who already has a legitimate operator record could submit an `UpdateNodeOperatorConfig` proposal with an inflated count and, if it passes, collect inflated rewards indefinitely until detected. The likelihood is **medium** given the governance barrier, but the impact is high enough to warrant a fix.

### Recommendation
1. In `calculate_rewards_v0` (or its callers), cross-reference `rewardable_nodes` against the actual count of `NodeRecord` entries in the registry that reference the given node operator, capping the reward at `min(rewardable_nodes[type], actual_node_count[type])`.
2. Add a registry invariant check that `rewardable_nodes[type] <= count of NodeRecords with this operator and this reward type` and enforce it in `do_update_node_operator_config` and `do_add_node_operator`.
3. Accelerate the migration to the performance-based reward path (`get_rewardable_nodes_per_provider`), which already iterates over actual `NodeRecord` entries and is not susceptible to this inflation.

### Proof of Concept

**Entry path (unprivileged governance participant):**
1. Attacker controls a neuron with enough voting power (or colludes with other neuron holders) to pass an `UpdateNodeOperatorConfig` proposal.
2. Proposal sets `rewardable_nodes = {"type3": 1000}` for the attacker's node operator, while the operator actually runs 0 or a small number of nodes.
3. The proposal passes NNS governance and is applied to the registry canister via `do_update_node_operator_config`.
4. At the next monthly reward cycle, `mint_monthly_node_provider_rewards` is called by the governance heartbeat.
5. `get_monthly_node_provider_rewards` calls `get_node_providers_monthly_xdr_rewards` on the registry canister (or node rewards canister).
6. `calculate_rewards_v0` reads `node_operator.rewardable_nodes["type3"] = 1000` and computes rewards for 1,000 type3 nodes.
7. The governance canister mints the corresponding ICP to the attacker's reward account via `mint_reward_to_neuron_or_account`.

**Relevant code path:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

The performance-based path that correctly iterates actual node records (and is not vulnerable) is: [7](#0-6)

### Citations

**File:** rs/registry/node_provider_rewards/src/lib.rs (L60-75)
```rust
        for (node_type, node_count) in node_operator.rewardable_nodes.iter() {
            let rate = match rewards_table.get_rate(region, node_type) {
                Some(rate) => rate,
                None => {
                    np_log.add_entry(LogEntry::RateNotFoundInRewardTable {
                        region: region.clone(),
                        node_type: node_type.clone(),
                        node_operator_id,
                    });

                    NodeRewardRate {
                        xdr_permyriad_per_node_per_month: 1,
                        reward_coefficient_percent: Some(100),
                    }
                }
            };
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

**File:** rs/protobuf/def/registry/node_operator/v1/node_operator.proto (L27-28)
```text
  // Provider should be rewarded.
  map<string, uint32> rewardable_nodes = 5;
```

**File:** rs/nns/governance/src/governance.rs (L4067-4075)
```rust
        let monthly_node_provider_rewards = if are_performance_based_rewards_enabled() {
            self.get_node_providers_rewards().await?
        } else {
            self.get_monthly_node_provider_rewards().await?
        };

        let _ = self
            .reward_node_providers(&monthly_node_provider_rewards.rewards)
            .await;
```

**File:** rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs (L19-60)
```rust
    pub fn get_node_providers_monthly_xdr_rewards(
        &self,
        request: GetNodeProvidersMonthlyXdrRewardsRequest,
    ) -> Result<NodeProvidersMonthlyXdrRewards, String> {
        let mut rewards = NodeProvidersMonthlyXdrRewards::default();

        let version = request.registry_version.unwrap_or(self.latest_version());

        let rewards_table_bytes = self
            .get(NODE_REWARDS_TABLE_KEY.as_bytes(), version)
            .ok_or_else(|| "Node Rewards Table was not found in the Registry".to_string())?
            .value
            .clone();

        let rewards_table = NodeRewardsTable::decode(rewards_table_bytes.as_slice()).unwrap();

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
