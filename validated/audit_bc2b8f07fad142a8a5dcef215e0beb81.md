### Title
Node Provider Reward Gaming via Lexicographic Node Operator Key Ordering - (`rs/registry/node_provider_rewards/src/lib.rs`)

### Summary

The `calculate_rewards_v0` function processes Node Operator records in lexicographic order of their registry keys (which embed the Node Operator's principal ID). For type3 nodes, a running decay coefficient is applied sequentially across all of a Node Provider's data centers in the same country. A Node Provider controlling multiple Node Operators in the same country can maximize their total rewards by choosing Node Operator principal IDs that ensure their highest-rate data center is processed first, receiving the full (undecayed) coefficient.

### Finding Description

In `rs/registry/node_provider_rewards/src/lib.rs`, `calculate_rewards_v0` iterates over node operators in the order supplied by the caller: [1](#0-0) 

The caller in `rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs` collects node operators via `get_key_family_iter_at_version`, which iterates the registry's internal `BTreeMap` store in lexicographic byte order of the key: [2](#0-1) [3](#0-2) 

The registry key for a Node Operator is `node_operator_record_` + the text representation of the Node Operator's principal ID: [4](#0-3) 

For type3 nodes, a running coefficient `np_coeff` starts at `1.0` and is multiplied by `dc_reward_coefficient_percent` after each node processed for the same `(node_provider_id, continent, country)` key: [5](#0-4) 

The code itself explicitly acknowledges this ordering dependency as a known issue: [6](#0-5) 

A Node Provider who controls multiple Node Operators in the same country can generate key pairs offline and select the principal ID whose text representation sorts lexicographically first, ensuring their highest-rate data center is processed first and receives the full (undecayed) coefficient.

### Impact Explanation

For a Node Provider with two data centers in the same country, with rates R1 > R2 and decay coefficient C < 1:
- Processing DC1 (rate R1) first: total = R1·1.0 + R2·C
- Processing DC2 (rate R2) first: total = R2·1.0 + R1·C

Since R1 > R2 and C < 1, the first ordering always yields higher total rewards: R1(1−C) > R2(1−C). The difference grows with the number of nodes and the magnitude of the decay. With typical decay coefficients of 70–98%, this can represent a meaningful and persistent financial advantage compounding every monthly reward cycle.

### Likelihood Explanation

Generating a principal ID that sorts lexicographically before a target is computationally trivial — a Node Provider simply generates key pairs offline until they find one whose text-encoded principal ID is lexicographically smaller than their other Node Operator's key. This requires no privileged access beyond what a legitimate Node Provider already has: the ability to submit governance proposals to add Node Operators with self-chosen principal IDs. The governance process approves the addition of the Node Operator role, not the specific principal ID chosen.

### Recommendation

The ordering of Node Operator records should not affect the total rewards a Node Provider receives. Options include:

1. **Sort by rate descending before applying decay** (as done in the V2 performance-based algorithm at `rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs` line 483): [7](#0-6) 
2. **Aggregate all nodes for a given NP+country first**, then apply the decay in a canonical order independent of Node Operator key ordering.
3. **Distribute the average reward** across all nodes in the group (as V1 of the performance-based algorithm does), making the result order-independent.

### Proof of Concept

1. Node Provider NP controls two Node Operators in Switzerland:
   - NO_A (principal `aaaa-...`) → DC_A, type3, rate = 22,000,000 XDR/month
   - NO_B (principal `zzzz-...`) → DC_B, type3, rate = 10,000,000 XDR/month
   - Decay coefficient = 70%

2. Lexicographic order: `node_operator_record_aaaa-...` < `node_operator_record_zzzz-...`, so NO_A is processed first.

3. Rewards: DC_A gets 22,000,000 × 1.0 = 22,000,000; DC_B gets 10,000,000 × 0.7 = 7,000,000. Total = **29,000,000**.

4. If NP had chosen principal IDs in reverse order (NO_B first): DC_B gets 10,000,000 × 1.0 = 10,000,000; DC_A gets 22,000,000 × 0.7 = 15,400,000. Total = **25,400,000**.

5. By choosing the principal ID for their high-rate DC to sort first, NP gains **3,600,000 XDR/month** (~14% more) with zero additional infrastructure cost.

### Citations

**File:** rs/registry/node_provider_rewards/src/lib.rs (L30-30)
```rust
    for (key_string, node_operator) in node_operators.iter() {
```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L85-99)
```rust
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
```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L117-139)
```rust
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
```

**File:** rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs (L35-40)
```rust
        let node_operators = get_key_family_iter_at_version::<NodeOperatorRecord>(
            self,
            NODE_OPERATOR_RECORD_KEY_PREFIX,
            version,
        )
        .collect::<Vec<_>>();
```

**File:** rs/registry/canister/src/common/key_family.rs (L56-60)
```rust
    // Note, using the 'store' which is a BTreeMap is what guarantees the order of keys.
    registry
        .store
        .range(start..)
        .take_while(|(k, _)| k.starts_with(prefix_bytes))
```

**File:** rs/registry/keys/src/lib.rs (L32-32)
```rust
pub const NODE_OPERATOR_RECORD_KEY_PREFIX: &str = "node_operator_record_";
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L482-483)
```rust
                    // Sort entries first by Base Reward (Desc) then by Coefficient (Desc) to process high-value nodes first.
                    entries.sort_by(|(r1, c1), (r2, c2)| r2.cmp(r1).then_with(|| c2.cmp(c1)));
```
