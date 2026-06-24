### Title
Data Center Removal via Governance Blocks All Node Provider Monthly Reward Calculations - (`rs/registry/node_provider_rewards/src/lib.rs`)

### Summary
A governance proposal that removes a data center record from the registry (via `AddOrRemoveDataCentersProposalPayload`) while node operators still reference that data center causes `calculate_rewards_v0` to return a hard error, propagating through `get_node_providers_monthly_xdr_rewards` and blocking the entire monthly node provider reward calculation for all providers — not just those associated with the removed data center.

### Finding Description

`calculate_rewards_v0` in `rs/registry/node_provider_rewards/src/lib.rs` iterates over every node operator record and looks up its associated data center:

```rust
let dc = data_centers.get(&node_operator.dc_id).ok_or_else(|| {
    format!(
        "Node Operator with key '{}' has data center ID '{}' \
                    not found in the Registry",
        node_operator_id, node_operator.dc_id
    )
})?;
``` [1](#0-0) 

The `?` operator propagates the `Err` immediately out of `calculate_rewards_v0`, which returns `Result<RewardsPerNodeProvider, String>`. This error then propagates through the caller:

```rust
let reward_values = calculate_rewards_v0(&rewards_table, &node_operators, &data_centers)?;
``` [2](#0-1) 

So `get_node_providers_monthly_xdr_rewards` also returns `Err`, blocking the entire monthly reward computation for **all** node providers — not just those in the affected data center.

The `UpdateNodeRewardsTableProposalPayload` only ever adds or extends entries (via `NodeRewardsTable::extend`), never removes them: [3](#0-2) 

However, data centers are a separate registry key family (`DATA_CENTER_KEY_PREFIX`) and can be removed via `AddOrRemoveDataCentersProposalPayload` through NNS governance. There is no guard in `calculate_rewards_v0` to handle a missing data center gracefully — it is a hard failure that aborts the entire computation.

The `NodeRewardsTable::get_rate` function, by contrast, handles missing entries gracefully by returning `None` and falling back to a default rate of 1 XDR: [4](#0-3) 

The data center lookup has no equivalent fallback.

### Impact Explanation

**Impact: High.** If any single node operator in the registry references a data center that has been removed, the entire `get_node_providers_monthly_xdr_rewards` call fails. This blocks monthly node provider reward distribution for **all** node providers on the Internet Computer, not just those associated with the removed data center. Node providers cannot receive their XDR-denominated rewards until the data center record is restored or the referencing node operator records are updated.

### Likelihood Explanation

**Likelihood: Low.** Triggering this requires an NNS governance proposal to remove a data center record while at least one node operator still references it. This could happen accidentally (a legitimate governance action that overlooks the dependency) or through a malicious governance proposal. The NNS governance process requires a majority of voting power, making deliberate exploitation difficult, but accidental triggering is plausible during routine registry maintenance.

### Recommendation

1. **Make the data center lookup non-fatal**: Instead of using `?` to propagate the error, log a warning and skip the node operator (or assign a default reward of 0), consistent with how missing reward table entries are handled.
2. **Add a registry invariant check**: Before applying a data center removal mutation, verify that no node operator record references the data center being removed. Reject the mutation if any such reference exists.
3. **Decouple live registry state from historical reward calculations**: Cache the data center → region mapping at the time of reward calculation so that subsequent registry changes do not retroactively break reward computations.

### Proof of Concept

1. NNS governance passes an `AddOrRemoveDataCentersProposalPayload` that removes data center `"dc-xyz"` from the registry.
2. At least one `NodeOperatorRecord` in the registry still has `dc_id = "dc-xyz"`.
3. The next monthly reward cycle triggers `get_node_providers_monthly_xdr_rewards`.
4. `calculate_rewards_v0` iterates node operators, reaches the operator with `dc_id = "dc-xyz"`, calls `data_centers.get("dc-xyz")` which returns `None`, and the `ok_or_else(...)?` propagates `Err("Node Operator with key '...' has data center ID 'dc-xyz' not found in the Registry")`.
5. `get_node_providers_monthly_xdr_rewards` returns `Err(...)`, and the NNS governance canister's monthly reward distribution fails for all node providers. [1](#0-0) [5](#0-4)

### Citations

**File:** rs/registry/node_provider_rewards/src/lib.rs (L46-52)
```rust
        let dc = data_centers.get(&node_operator.dc_id).ok_or_else(|| {
            format!(
                "Node Operator with key '{}' has data center ID '{}' \
                            not found in the Registry",
                node_operator_id, node_operator.dc_id
            )
        })?;
```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L61-74)
```rust
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
```

**File:** rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs (L19-59)
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
```

**File:** rs/registry/canister/src/mutations/do_update_node_rewards_table.rs (L13-31)
```rust
    pub fn do_update_node_rewards_table(&mut self, payload: UpdateNodeRewardsTableProposalPayload) {
        println!("{}do_update_node_rewards_table: {:?}", LOG_PREFIX, &payload);

        let mut node_rewards_table = self
            .get(NODE_REWARDS_TABLE_KEY.as_bytes(), self.latest_version())
            .map(|RegistryValue { value, .. }| NodeRewardsTable::decode(value.as_slice()).unwrap())
            .unwrap_or_default();

        node_rewards_table.extend(payload.get_rewards_table());

        let mutations = vec![RegistryMutation {
            mutation_type: registry_mutation::Type::Upsert as i32,
            key: NODE_REWARDS_TABLE_KEY.into(),
            value: node_rewards_table.encode_to_vec(),
        }];

        // Check invariants before applying mutations
        self.maybe_apply_mutation_internal(mutations);
    }
```
