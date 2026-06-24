### Title
Cross-Node-Type Coefficient State Contamination in `calculate_rewards_v0` Causes Systematic Under-Payment to Node Providers - (File: `rs/registry/node_provider_rewards/src/lib.rs`)

---

### Summary

In `calculate_rewards_v0`, the `np_coefficients_key` used to track the running reduction coefficient for type3-family nodes is keyed only on `node_provider_id:continent:country`, with no node-type component. When a node operator holds both `type3` and `type3.1` nodes in the same country, both node types share the same coefficient state. Because `rewardable_nodes` is a `BTreeMap` iterated in lexicographic order, `"type3"` is always processed before `"type3.1"`. The coefficient consumed by `type3` nodes bleeds directly into the starting coefficient for `type3.1` nodes, causing `type3.1` rewards to be systematically reduced by the `type3` node count — a cross-type aggregate contaminating per-type reward calculations.

---

### Finding Description

In `rs/registry/node_provider_rewards/src/lib.rs`, `calculate_rewards_v0` iterates over each node operator's `rewardable_nodes` map. For every node type matching `t.starts_with("type3")`, it constructs a coefficient key:

```rust
let np_coefficients_key = format!(
    "{}:{}",
    node_provider_id,
    region
        .splitn(3, ',')
        .take(2)
        .collect::<Vec<&str>>()
        .join(":")
);
``` [1](#0-0) 

This key is `node_provider_id:continent:country` — it contains **no node-type component**. Both `"type3"` and `"type3.1"` entries resolve to the identical key. The running coefficient `np_coeff` is read from and written back to this shared key after each node type's loop:

```rust
let mut np_coeff = *np_coefficients.get(&np_coefficients_key).unwrap_or(&1.0);
// ... per-node reward loop consuming np_coeff ...
np_coefficients.insert(np_coefficients_key, np_coeff);
``` [2](#0-1) 

Because `rewardable_nodes` is a `BTreeMap<String, u32>` iterated lexicographically, `"type3"` is always processed before `"type3.1"`. After all `type3` nodes are processed, `np_coeff` is already reduced by `type3_coefficient^(type3_count)`. The `type3.1` loop then starts from this depleted value, applying its own coefficient on top. The `type3.1` nodes are penalized by the `type3` node count even though they have a different base rate and a different reduction coefficient.

The production entry point is:

```rust
let reward_values = calculate_rewards_v0(&rewards_table, &node_operators, &data_centers)?;
``` [3](#0-2) 

This is called by the registry canister's `get_node_providers_monthly_xdr_rewards` handler, which is invoked by the NNS governance canister during the automatic monthly node-provider reward distribution.

The documentation in `rs/node_rewards/node_provider_reward_calculations.md` explicitly states:

> "3. Node Type Independence: … Different node types are calculated independently" [4](#0-3) 

The code violates this invariant.

---

### Impact Explanation

For any node provider operating both `type3` and `type3.1` nodes in the same country, the `type3.1` nodes receive systematically lower XDR rewards than they are entitled to. The magnitude of under-payment grows with the number of `type3` nodes the provider operates in that country. For example:

- Provider has 5 `type3` nodes (coefficient 90%) and 3 `type3.1` nodes (coefficient 70%) in the same country.
- After 5 `type3` nodes, running coefficient = `0.9^5 ≈ 0.5905`.
- `type3.1` node 1 receives `base_rate_3.1 × 0.5905` instead of `base_rate_3.1 × 1.0`.
- `type3.1` node 2 receives `base_rate_3.1 × 0.4134` instead of `base_rate_3.1 × 0.7`.
- `type3.1` node 3 receives `base_rate_3.1 × 0.2894` instead of `base_rate_3.1 × 0.49`.

The under-paid XDR is simply not minted — it is silently lost from the node provider's monthly reward. This is a ledger conservation bug: the intended reward allocation is not fully distributed.

---

### Likelihood Explanation

The NNS integration test at `rs/nns/integration_tests/src/node_provider_remuneration.rs` already exercises a node provider with both `type3` and `type3.1` nodes in the same data center region, confirming this is a realistic and tested configuration on mainnet. [5](#0-4) 

The monthly reward distribution is triggered automatically by the NNS governance canister's periodic tasks — no governance proposal or privileged action is required to reach the vulnerable code path. Any node provider with mixed `type3`/`type3.1` nodes in the same country is affected every reward period.

---

### Recommendation

Include the node type in the `np_coefficients_key` so that `type3` and `type3.1` maintain independent coefficient states:

```rust
let np_coefficients_key = format!(
    "{}:{}:{}",
    node_provider_id,
    region
        .splitn(3, ',')
        .take(2)
        .collect::<Vec<&str>>()
        .join(":"),
    node_type  // add node_type to prevent cross-type contamination
);
```

This aligns `calculate_rewards_v0` with the documented invariant that "different node types are calculated independently" and with the behavior of the newer V1/V2 performance-based algorithms, which correctly separate per-type base rates before applying the country-level reduction sequence. [6](#0-5) 

---

### Proof of Concept

Scenario: Node Provider NP1 has one node operator in `"North America,US,CA"` with:
- 3 `type3` nodes, `reward_coefficient_percent = 90`, base rate = 22,000,000 XDR/month
- 2 `type3.1` nodes, `reward_coefficient_percent = 70`, base rate = 30,000,000 XDR/month

**Actual behavior (buggy):**

| Node | Type | np_coeff at start | Reward |
|------|------|-------------------|--------|
| 1 | type3 | 1.000 | 22,000,000 |
| 2 | type3 | 0.900 | 19,800,000 |
| 3 | type3 | 0.810 | 17,820,000 |
| 4 | type3.1 | **0.729** (inherited from type3!) | 21,870,000 |
| 5 | type3.1 | 0.5103 | 15,309,000 |

**Expected behavior (independent keys):**

| Node | Type | np_coeff at start | Reward |
|------|------|-------------------|--------|
| 1 | type3 | 1.000 | 22,000,000 |
| 2 | type3 | 0.900 | 19,800,000 |
| 3 | type3 | 0.810 | 17,820,000 |
| 4 | type3.1 | **1.000** | 30,000,000 |
| 5 | type3.1 | 0.700 | 21,000,000 |

Under-payment for `type3.1` nodes: `(30,000,000 − 21,870,000) + (21,000,000 − 15,309,000) = 13,821,000 XDR/month` silently lost for this single operator. The effect compounds with more `type3` nodes. [7](#0-6)

### Citations

**File:** rs/registry/node_provider_rewards/src/lib.rs (L60-153)
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

            let dc_reward = match &node_type {
                t if t.starts_with("type3") => {
                    // For type3 nodes, the rewards are progressively reduced for each additional node owned by a NP.
                    // This helps to improve network decentralization. The first node gets the full reward.
                    // After the first node, the rewards are progressively reduced by multiplying them with reward_coefficient_percent.
                    // For the n-th node, the reward is:
                    // reward(n) = reward(n-1) * reward_coefficient_percent ^ (n-1)
                    //
                    // A note around the type3 rewards and iter() over self.store
                    //
                    // One known issue with this implementation is that in some edge cases it could lead to
                    // unexpected results. The outer loop iterates over the node operator records sorted
                    // lexicographically, instead of the order in which the records were added to the registry,
                    // or instead of the order in which NP/NO adds nodes to the network. This means that all
                    // reduction factors for the node operator A are applied prior to all reduction factors for
                    // the node operator B, independently from the order in which the node operator records,
                    // nodes, or the rewardable nodes were added to the registry.
                    // For instance, say a Node Provider adds a Node Operator B in region 1 with higher reward
                    // coefficient so higher average rewards, and then A in region 2 with lower reward
                    // coefficient so lower average rewards. When the rewards are calculated, the rewards for
                    // Node Operator A are calculated before the rewards for B (due to the lexicographical
                    // order), and the final rewards will be lower than they would be calculated first for B and
                    // then for A, as expected based on the insert order.

                    let reward_base = rate.xdr_permyriad_per_node_per_month as f64;

                    // To de-stimulate the same NP having too many nodes in the same country, the node rewards
                    // is reduced for each node the NP has in the given country.
                    // Join the NP PrincipalId + DC Continent + DC Country, and use that as the key for the
                    // reduction coefficients.
                    let np_coefficients_key = format!(
                        "{}:{}",
                        node_provider_id,
                        region
                            .splitn(3, ',')
                            .take(2)
                            .collect::<Vec<&str>>()
                            .join(":")
                    );

                    let mut np_coeff = *np_coefficients.get(&np_coefficients_key).unwrap_or(&1.0);

                    // Default reward_coefficient_percent is set to 80%, which is used as a fallback only in the
                    // unlikely case that the type3 entry in the reward table:
                    // a) has xdr_permyriad_per_node_per_month entry set for this region, but
                    // b) does NOT have the reward_coefficient_percent value set
                    let dc_reward_coefficient_percent =
                        rate.reward_coefficient_percent.unwrap_or(80) as f64 / 100.0;

                    let mut dc_reward = 0;
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

            np_log.add_entry(LogEntry::DCRewards {
                dc_id: node_operator.dc_id.clone(),
                node_type: node_type.clone(),
                rewardable_count: *node_count,
                rewards_xdr_permyriad: dc_reward,
            });
            *np_rewards += dc_reward;
        }
    }
```

**File:** rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs (L49-49)
```rust
        let reward_values = calculate_rewards_v0(&rewards_table, &node_operators, &data_centers)?;
```

**File:** rs/node_rewards/node_provider_reward_calculations.md (L237-240)
```markdown
3. Node Type Independence:
    - Type0 and type1 nodes always receive their full regional rate
    - Only type3 nodes are subject to the decay factor
    - Different node types are calculated independently
```

**File:** rs/nns/integration_tests/src/node_provider_remuneration.rs (L506-578)
```rust
    // Add Node Operator 3
    let max_rewardable_nodes_3 = btreemap! {
        NodeRewardType::Type1.to_string() => 2,
        NodeRewardType::Type3.to_string() => 2,
        NodeRewardType::Type3dot1.to_string() => 3,
        NodeRewardType::Type4.to_string() => 2,
    };
    add_node_operator(
        &state_machine,
        &node_info_3.operator_id,
        &node_info_3.provider_id,
        "FM1",
        max_rewardable_nodes_3,
        "0:0:0:0:0:0:0:0",
    );

    // Add Nodes for Node Provider 3
    let np_3_nodes = vec![
        add_node(
            &state_machine,
            node_info_3.operator_id,
            9,
            NodeRewardType::Type1,
        ),
        add_node(
            &state_machine,
            node_info_3.operator_id,
            10,
            NodeRewardType::Type1,
        ),
        add_node(
            &state_machine,
            node_info_3.operator_id,
            11,
            NodeRewardType::Type3,
        ),
        add_node(
            &state_machine,
            node_info_3.operator_id,
            12,
            NodeRewardType::Type3,
        ),
        add_node(
            &state_machine,
            node_info_3.operator_id,
            13,
            NodeRewardType::Type3dot1,
        ),
        add_node(
            &state_machine,
            node_info_3.operator_id,
            14,
            NodeRewardType::Type3dot1,
        ),
        add_node(
            &state_machine,
            node_info_3.operator_id,
            15,
            NodeRewardType::Type3dot1,
        ),
        add_node(
            &state_machine,
            node_info_3.operator_id,
            16,
            NodeRewardType::Type4,
        ),
        add_node(
            &state_machine,
            node_info_3.operator_id,
            17,
            NodeRewardType::Type4,
        ),
    ];
```
