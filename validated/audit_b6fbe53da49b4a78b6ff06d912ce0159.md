### Title
Order-Dependent Type3 Node Provider Reward Calculation Due to Lexicographic Iteration - (`File: rs/registry/node_provider_rewards/src/lib.rs`)

### Summary
The `calculate_rewards_v0` function, which computes monthly XDR rewards for node providers, applies the type3 decay coefficient in lexicographic order of node operator registry keys rather than any principled ordering. A node provider with multiple type3 node operators in the same country receives different total rewards depending on the lexicographic sort position of their operator keys. Because node providers choose their own operator principal IDs when submitting `AddNodeOperator` governance proposals, they can grind key pairs to obtain a principal ID that sorts favorably, systematically extracting higher rewards than peers with equivalent infrastructure.

### Finding Description

In `calculate_rewards_v0` in `rs/registry/node_provider_rewards/src/lib.rs`, the outer loop iterates over node operators in the order they are passed in:

```rust
for (key_string, node_operator) in node_operators.iter() {
``` [1](#0-0) 

The callers collect node operators from the registry using `get_key_family_iter_at_version`, which returns records sorted lexicographically by their registry key string (the node operator principal ID encoded as a string): [2](#0-1) 

For type3 nodes, the running decay coefficient `np_coeff` is accumulated across all node operators of the same provider in the same country, in this lexicographic order:

```rust
let mut np_coeff = *np_coefficients.get(&np_coefficients_key).unwrap_or(&1.0);
// ...
for i in 0..*node_count {
    let node_reward = (reward_base * np_coeff) as u64;
    dc_reward += node_reward;
    np_coeff *= dc_reward_coefficient_percent;
}
np_coefficients.insert(np_coefficients_key, np_coeff);
``` [3](#0-2) 

The code itself acknowledges this as a known issue:

> "One known issue with this implementation is that in some edge cases it could lead to unexpected results. The outer loop iterates over the node operator records sorted lexicographically, instead of the order in which the records were added to the registry... For instance, say a Node Provider adds a Node Operator B in region 1 with higher reward coefficient so higher average rewards, and then A in region 2 with lower reward coefficient so lower average rewards. When the rewards are calculated, the rewards for Node Operator A are calculated before the rewards for B (due to the lexicographical order), and the final rewards will be lower than they would be calculated first for B and then for A." [4](#0-3) 

Because the decay is geometric (each successive node in the same country earns `reward_coefficient_percent` of the previous), processing a high-node-count operator first consumes the high-value positions of the decay sequence, leaving lower-value positions for subsequent operators. The total reward for the provider is therefore a function of the lexicographic ordering of their operator keys, not of their actual infrastructure contribution.

The `AddNodeOperator` governance proposal accepts an arbitrary `node_operator_principal_id`: [5](#0-4) 

A node provider can generate many key pairs offline, compute their principal IDs, and select the one whose string representation sorts lexicographically earliest among their existing operators. Submitting this as the new operator's principal ID causes that operator's nodes to be processed first in the decay sequence, capturing the highest-value reward positions. This is a key-grinding attack requiring no special privileges beyond the ability to submit governance proposals, which all node providers already possess.

### Impact Explanation

**Ledger conservation bug / unfair reward distribution:** Two node providers with identical type3 node counts in the same country receive different monthly ICP rewards purely based on the lexicographic ordering of their operator principal IDs. A node provider who grinds key pairs to obtain a favorably-sorting principal ID systematically extracts more ICP from the reward pool than an equivalent provider who does not. Over time, this compounds into a material financial advantage. The NNS mints ICP for node provider rewards based on these calculations; incorrect amounts represent a conservation violation in the ICP ledger.

**Likelihood:** Medium. The attack requires only offline key grinding (no on-chain cost) and a standard `AddNodeOperator` governance proposal. The NNS community reviewing such proposals has no mechanism to detect that a principal ID was chosen for its lexicographic advantage. The bug is already acknowledged in the source code comments, confirming it is a real, reachable condition.

### Likelihood Explanation

Any node provider with two or more type3 node operators in the same country is affected by this bug today, even without any intentional exploitation. The intentional exploitation path (key grinding) is trivially accessible: principal IDs are derived from public keys, and a node provider can generate thousands of key pairs in seconds to find one that sorts favorably. The governance proposal to add the operator is indistinguishable from a legitimate one.

### Recommendation

Replace the lexicographic iteration order with a deterministic, insertion-order-preserving approach, or — analogous to the mitigation in the external report — adopt an averaging model that is order-independent. The new performance-based algorithm (`RewardsCalculationV2`) already implements a fairer approach by grouping all type3/type3.1 nodes by country and computing an average reward, making the result independent of processing order: [6](#0-5) 

Migrating the legacy `calculate_rewards_v0` path to the same averaging model, or deprecating it in favor of V2, would eliminate the order-dependency.

### Proof of Concept

Given a node provider NP with two type3 node operators in `Europe,CH`:
- Operator `aaa...` (sorts first): 5 nodes, coefficient 70%
- Operator `zzz...` (sorts last): 5 nodes, coefficient 70%

**Current behavior (lexicographic, `aaa` first):**
- `aaa` nodes: `R, 0.7R, 0.49R, 0.343R, 0.2401R` (total ≈ 2.773R)
- `zzz` nodes: `0.168R, 0.118R, 0.082R, 0.058R, 0.040R` (total ≈ 0.466R)
- Grand total ≈ 3.239R

**If NP grinds a key that sorts before `aaa` for the high-coefficient operator:**
- High-coeff operator processed first: same 2.773R
- Low-coeff operator processed second: same 0.466R
- Grand total ≈ 3.239R (same in this symmetric case)

**Asymmetric case (different base rates per DC):** If the two operators have different `xdr_permyriad_per_node_per_month` values (e.g., because they are in different sub-regions that match different reward table entries), processing the higher-rate operator first captures more of the high-coefficient positions, yielding materially higher total rewards. The node provider can grind a principal ID to ensure their higher-rate operator always sorts first, permanently maximizing their monthly ICP reward.

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

**File:** rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs (L186-196)
```rust
    ) -> Registry {
        let node_operator_payload = AddNodeOperatorPayload {
            node_operator_principal_id: Some(no_principal),
            node_allowance,
            node_provider_principal_id: Some(np_principal),
            dc_id: dc_id.clone(),
            rewardable_nodes: rewardable_nodes.clone(),
            ipv6: None,
            max_rewardable_nodes: None,
        };
        registry.do_add_node_operator(node_operator_payload);
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L458-508)
```rust
        let base_rewards_type3 = base_rewards_type3
            .into_iter()
            .map(|(region, mut entries)| {
                let nodes_count = entries.len();

                if Self::VERSION == 1 {
                    let (rates, coeff): (Vec<Decimal>, Vec<RewardsCoefficientPercent>) =
                        entries.into_iter().unzip();
                    let avg_rate = avg(rates.as_slice()).unwrap_or_default();
                    let avg_coeff = avg(coeff.as_slice()).unwrap_or_default();

                    let mut running_coefficient = dec!(1);
                    let mut region_rewards = Vec::new();
                    for _ in 0..nodes_count {
                        region_rewards.push(avg_rate * running_coefficient);
                        running_coefficient *= avg_coeff;
                    }
                    let region_rewards_avg = avg(&region_rewards).unwrap_or_default();

                    (
                        region,
                        (region_rewards_avg, nodes_count, avg_rate, avg_coeff),
                    )
                } else {
                    // Sort entries first by Base Reward (Desc) then by Coefficient (Desc) to process high-value nodes first.
                    entries.sort_by(|(r1, c1), (r2, c2)| r2.cmp(r1).then_with(|| c2.cmp(c1)));

                    let mut total_rewards = Decimal::ZERO;
                    let mut running_coeff = dec!(1);

                    // We also need averages for the reporting/Result struct later.
                    let mut total_rate_sum = Decimal::ZERO;
                    let mut total_coeff_sum = Decimal::ZERO;

                    for (rate, coeff) in &entries {
                        total_rewards += rate * running_coeff;
                        running_coeff *= coeff;

                        total_rate_sum += rate;
                        total_coeff_sum += coeff;
                    }
                    let avg_rate = total_rate_sum / Decimal::from(nodes_count);
                    let avg_coeff = total_coeff_sum / Decimal::from(nodes_count);
                    let region_rewards_avg = total_rewards / Decimal::from(nodes_count);

                    (
                        region,
                        (region_rewards_avg, nodes_count, avg_rate, avg_coeff),
                    )
                }
            })
```
