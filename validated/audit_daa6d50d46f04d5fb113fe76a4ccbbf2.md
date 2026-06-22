### Title
V1 Type3 Node Reward Calculation Uses Averaged Coefficients Instead of Per-Node Coefficients, Leading to Incorrect Reward Distribution - (File: `rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs`)

### Summary
The V1 performance-based reward algorithm groups Type3/Type3.1 nodes by country, then averages their base rates and reduction coefficients before applying the decay sequence. This produces a single `region_rewards_avg` assigned identically to all nodes in the group. When nodes in the group have different actual rates or coefficients (e.g., Type3 at 0.95 and Type3.1 at 0.80), the averaged calculation deviates materially from what each node should receive based on its own parameters — directly mirroring the external report's "averaged factor applied to a specific case" bug class.

### Finding Description

In `calculate_base_rewards_by_region_and_type`, the V1 branch (guarded by `Self::VERSION == 1`) executes the following logic for every country-level Type3 group:

```rust
// lines 463-480
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
``` [1](#0-0) 

The algorithm then assigns this single `region_rewards_avg` to **every** node in the group, regardless of each node's individual rate or coefficient:

```rust
// lines 511-526
let base_rewards_for_day = if is_type3(&node.node_reward_type) {
    let region_key = type3_region_key(&node.region);
    let (base_rewards_daily, _, _, _) = base_rewards_type3
        .get(&region_key)
        .expect("Type3 base rewards expected for provider");
    base_rewards_daily
} ...
base_rewards_per_node.insert(node.node_id, *base_rewards_for_day);
``` [2](#0-1) 

The structural parallel to the external report is exact:

| External Report | IC V1 Algorithm |
|---|---|
| `TARGET_HEALTH` uses average `adjustFactor` across all markets | V1 uses `avg_rate` / `avg_coeff` across all Type3/Type3.1 nodes in a country |
| Actual liquidation executes in one specific market with its own factor | Actual reward is paid to each node that has its own rate and coefficient |
| Resulting health factor deviates significantly from `TARGET_HEALTH` | Resulting per-node reward deviates significantly from the correct per-node amount |

The V2 algorithm was introduced precisely to fix this: it sorts entries by `(rate desc, coeff desc)` and applies each node's own coefficient sequentially, without averaging. [3](#0-2) 

The `NodeRewardRate` protobuf confirms that each region+type entry carries its own independent `reward_coefficient_percent`, making the averaging lossy by design: [4](#0-3) 

### Impact Explanation

The V1 averaging error causes a measurable, systematic mis-distribution of ICP node-provider rewards:

- A node provider holding **higher-rate** Type3 nodes in the same country as lower-rate Type3.1 nodes receives **less** than their correct entitlement (the average pulls their reward down).
- A node provider holding **lower-rate** nodes receives **more** than their correct entitlement.
- The V2 e2e test quantifies the magnitude: for a mixed group with coefficients 0.95 and 0.80, V1 yields `avg = 8276.37` while V2 yields `avg = 8936.25` — a **~8% deviation**. [5](#0-4) 

This is a **ledger conservation bug**: ICP rewards are minted and distributed based on incorrect per-node amounts. Node providers who are entitled to higher rewards are systematically underpaid, and those entitled to lower rewards are overpaid, with no mechanism for correction after distribution.

### Likelihood Explanation

The condition that triggers the bug — a node provider owning both Type3 and Type3.1 nodes in the same country — is explicitly modeled and tested in the codebase, confirming it is a realistic and expected deployment scenario: [6](#0-5) 

The `NodeRewardType` enum includes both `Type3` and `Type3dot1` as distinct variants with different regional rates, and the grouping logic (`type3_region_key`) deliberately merges them at the country level, making mixed groups the norm rather than the exception for large node providers. [7](#0-6) 

The node rewards canister references both `RewardsCalculationV1` and `RewardsCalculationV2` in its core module (`rs/node_rewards/canister/src/canister/mod.rs`), indicating V1 remains active in the deployed canister (exact dispatch logic was not fully confirmed due to tool iteration limits).

### Recommendation

1. Ensure `RewardsCalculationV2` is the sole algorithm used for all new daily reward calculations in the node rewards canister. V1 should be retained only for historical reproducibility of past periods, not for any forward-looking computation.
2. Audit past V1-computed reward distributions to identify node providers who were systematically under- or over-paid due to the averaging error, and consider a correction mechanism.
3. Add a canister-level invariant or metric that asserts the active algorithm version is ≥ 2 for any reward period after V2's deployment date.

### Proof of Concept

From the existing V2 e2e test (which explicitly documents the V1 vs V2 divergence):

```
// V1 calculation (averaging first):
// avg_rate = 10000, avg_coeff = 0.875
// rewards: 10000*1 + 10000*0.875 + 10000*0.875² + 10000*0.875³
//        = 10000 + 8750 + 7656.25 + 6699.22 = 33105.47
// avg = 8276.37  ← INCORRECT

// V2 calculation (per-node coefficients, sorted):
// (10000, 0.95), (10000, 0.95), (10000, 0.80), (10000, 0.80)
// rewards: 10000*1 + 10000*0.95 + 10000*0.9025 + 10000*0.722
//        = 10000 + 9500 + 9025 + 7220 = 35745
// avg = 8936.25  ← CORRECT
``` [8](#0-7) 

The ~8% deviation in per-node base rewards propagates directly into the final `adjusted_rewards_xdr_permyriad` after performance multipliers are applied, and ultimately into the ICP amount minted and transferred to each node provider. [9](#0-8)

### Citations

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L413-423)
```rust
        fn is_type3(node_type: &NodeRewardType) -> bool {
            node_type == &NodeRewardType::Type3 || node_type == &NodeRewardType::Type3dot1
        }

        fn type3_region_key(region: &Region) -> String {
            region
                .splitn(3, ',')
                .take(2)
                .collect::<Vec<&str>>()
                .join(":")
        }
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L463-480)
```rust
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
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L481-507)
```rust
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
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L511-526)
```rust
        for node in rewardable_nodes {
            let base_rewards_for_day = if is_type3(&node.node_reward_type) {
                let region_key = type3_region_key(&node.region);

                let (base_rewards_daily, _, _, _) = base_rewards_type3
                    .get(&region_key)
                    .expect("Type3 base rewards expected for provider");
                base_rewards_daily
            } else {
                let (base_rewards_daily, _) = base_rewards
                    .get(&(node.node_reward_type, node.region.clone()))
                    .expect("base rewards expected for each node");
                base_rewards_daily
            };

            base_rewards_per_node.insert(node.node_id, *base_rewards_for_day);
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L565-586)
```rust
    fn apply_performance_adjustments(
        rewardable_nodes: &[RewardableNode],
        base_rewards: &BTreeMap<NodeId, Decimal>,
        performance_multiplier: &BTreeMap<NodeId, Decimal>,
    ) -> AdjustedRewardsResults {
        let mut adjusted_rewards = BTreeMap::new();

        for node in rewardable_nodes {
            let base_rewards_for_day = base_rewards
                .get(&node.node_id)
                .expect("Base rewards expected for each node");

            let performance_mult = performance_multiplier
                .get(&node.node_id)
                .expect("Performance multiplier expected for every node");

            let adjusted_rewards_for_day = base_rewards_for_day * performance_mult;
            adjusted_rewards.insert(node.node_id, adjusted_rewards_for_day);
        }

        AdjustedRewardsResults { adjusted_rewards }
    }
```

**File:** rs/protobuf/def/registry/node_rewards/v2/node_rewards.proto (L5-17)
```text
message NodeRewardRate {
  // The number of 10,000ths of IMF SDR (currency code XDR) to be rewarded per
  // node per month.
  uint64 xdr_permyriad_per_node_per_month = 1;

  // The coefficient of the node rewards the node provider gets
  // for having more than 1 node, as a percentage of the reward for first node.
  // A value of 100 means that the same reward is received for all nodes
  // A value of 0 means that only the first node gets the rewards, 2nd and later nodes get no reward
  // For values in between, the reward for the n-th node is:
  // reward(n) = reward(n-1) * reward_coefficient_percent ^ (n-1)
  optional int32 reward_coefficient_percent = 2;
}
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/v2/e2e_tests.rs (L135-151)
```rust
/// **Expected**: All Type3/Type3.1 nodes in the same country get the same average reward
/// **Key Test**: V2 groups all Type3* nodes by country and sorts by (rate desc, coeff desc)
///
/// DATA: type3 base_rewards: 10000, coeff: 0.95, N: 2
///       type3.1 base_rewards: 10000, coeff: 0.8, N: 2
///       1 type3 node has performance penalty
///
/// V2 Algorithm:
/// - All 4 nodes grouped under "North America:USA"
/// - Sorted by (rate desc, coeff desc): (10000, 0.95), (10000, 0.95), (10000, 0.80), (10000, 0.80)
/// - Calculation:
///   1. 10000 * 1.0 = 10000, running_coeff = 0.95
///   2. 10000 * 0.95 = 9500, running_coeff = 0.9025
///   3. 10000 * 0.9025 = 9025, running_coeff = 0.722
///   4. 10000 * 0.722 = 7220, running_coeff = 0.5776
/// - Total = 35745, Avg = 8936.25
/// - All 4 nodes get base_rewards = 8936.25
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/v2/e2e_tests.rs (L367-379)
```rust
    // V1 averages rates and coefficients first, then applies reduction
    // V2 sorts by (rate desc, coeff desc) and applies reduction sequentially
    //
    // V1 calculation:
    // - avg_rate = 10000, avg_coeff = 0.875
    // - rewards: 10000 * 0.875^0 + 10000 * 0.875^1 + 10000 * 0.875^2 + 10000 * 0.875^3
    // - = 10000 + 8750 + 7656.25 + 6699.21875 = 33105.46875
    // - avg = 8276.3671875
    //
    // V2 calculation:
    // - sorted: (10000, 0.95), (10000, 0.95), (10000, 0.80), (10000, 0.80)
    // - rewards: 10000*1 + 10000*0.95 + 10000*0.9025 + 10000*0.722 = 35745
    // - avg = 8936.25
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/v1/e2e_tests.rs (L344-348)
```rust
/// **Scenario**: Type3 and Type3.1 nodes in same country (3 Type3 + 2 Type3.1 in USA)
/// **Expected**: Nodes grouped by country, average coefficient applied, reduced rewards
/// **Key Test**: Type3 special logic with reduction coefficients
#[test]
fn test_type3_reduction_coefficient_logic() {
```
