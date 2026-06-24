### Title
Incorrect Separate Averaging of Rate and Coefficient in `RewardsCalculationV1` Produces Incorrect Type3 Node Provider Rewards - (File: `rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs`)

### Summary
`RewardsCalculationV1` (VERSION = 1) in the node rewards canister incorrectly averages base reward rates and reduction coefficients **separately** before combining them for Type3 node reward calculations. This is the direct IC analog of the Vader `TwapOracle.consult()` bug: two independent quantities are summed/averaged in isolation and then combined, instead of pairing each `(rate_i, coeff_i)` and processing them together. The algorithm is still callable via the node rewards canister public API and produces materially different (lower) rewards than the correct V2 algorithm.

### Finding Description

In `calculate_base_rewards_by_region_and_type`, when a node provider has Type3/Type3.1 nodes across multiple regions within the same country, the V1 branch (guarded by `Self::VERSION == 1`) does the following:

```rust
// V1 branch — lines 463-480
let (rates, coeff): (Vec<Decimal>, Vec<RewardsCoefficientPercent>) =
    entries.into_iter().unzip();
let avg_rate = avg(rates.as_slice()).unwrap_or_default();   // ← averages rates independently
let avg_coeff = avg(coeff.as_slice()).unwrap_or_default();  // ← averages coefficients independently

let mut running_coefficient = dec!(1);
let mut region_rewards = Vec::new();
for _ in 0..nodes_count {
    region_rewards.push(avg_rate * running_coefficient);   // ← combines averaged values
    running_coefficient *= avg_coeff;
}
let region_rewards_avg = avg(&region_rewards).unwrap_or_default();
``` [1](#0-0) 

This is mathematically equivalent to computing `(avg_rate) * (avg_coeff)^k` for each node `k`, which is **not** the same as computing `rate_i * coeff_i^k` for each actual `(rate_i, coeff_i)` pair. The error is identical in structure to the Vader bug: `(p1+p2)/2 * (q1+q2)/2 ≠ (p1*q1 + p2*q2)/2`.

The correct V2 branch (lines 481–507) sorts entries by `(rate desc, coeff desc)` and applies each node's actual rate and coefficient sequentially:

```rust
for (rate, coeff) in &entries {
    total_rewards += rate * running_coeff;   // ← each pair processed together
    running_coeff *= coeff;
}
``` [2](#0-1) 

The V2 e2e test explicitly documents the divergence between V1 and V2 for the same input:

```
// V1: avg_rate=10000, avg_coeff=0.875
//   rewards: 10000*1 + 10000*0.875 + 10000*0.875^2 + 10000*0.875^3 = 33105.46875
//   avg = 8276.3671875
//
// V2: sorted (10000,0.95),(10000,0.95),(10000,0.80),(10000,0.80)
//   rewards: 10000*1 + 10000*0.95 + 10000*0.9025 + 10000*0.722 = 35745
//   avg = 8936.25
``` [3](#0-2) 

V1 is still registered and callable in the node rewards canister's `calculate_rewards_for_date` dispatch table:

```rust
match rewards_calculator_version.version {
    RewardsCalculationV1::VERSION => {
        RewardsCalculationV1::calculate_rewards_for_date(date, &self)
    }
    RewardsCalculationV2::VERSION => {
        RewardsCalculationV2::calculate_rewards_for_date(date, &self)
    }
    ...
}
``` [4](#0-3) 

The default algorithm version is V2:

```rust
impl Default for RewardsCalculationAlgorithmVersion {
    fn default() -> Self {
        Self { version: RewardsCalculationV2::VERSION }
    }
}
``` [5](#0-4) 

### Impact Explanation

Any caller that invokes the node rewards canister with `algorithm_version = Some(RewardsCalculationAlgorithmVersion { version: 1 })` receives materially incorrect (understated) reward amounts for node providers operating Type3/Type3.1 nodes across multiple regions within the same country. The governance canister uses the node rewards canister output to compute and distribute actual ICP rewards to node providers:

```rust
let (rewards_per_node_provider, algorithm_version) = self
    .get_node_providers_xdr_permyriad_rewards(start_date, end_date)
    .await?;
``` [6](#0-5) 

If V1 is invoked (explicitly or via a misconfiguration), node providers with multi-region Type3 deployments receive less ICP than they are entitled to. The magnitude of the error grows with the diversity of `(rate, coeff)` pairs across regions — the more heterogeneous the node portfolio, the larger the discrepancy.

### Likelihood Explanation

V2 is the current default and the governance canister uses the default when calling the node rewards canister for monthly reward distribution. However, V1 remains a live, callable code path in the public canister API. Any canister caller or governance proposal that explicitly passes `algorithm_version = 1` will trigger the incorrect calculation. The node rewards canister accepts this parameter without restriction, and the V1 code path is not deprecated or access-controlled. The risk is that a future integration, a governance proposal, or a caller relying on V1 for "historical reproducibility" inadvertently uses V1 for a live reward cycle.

### Recommendation

1. **Remove V1 from the live dispatch table** or gate it behind an explicit "historical-only" flag that prevents its use for new reward periods.
2. **Alternatively**, fix the V1 aggregation to match V2's per-pair sequential approach, or document clearly that V1 is only valid for replaying historical calculations and must never be used for new reward distributions.
3. Add a canister-level guard that rejects `algorithm_version = 1` for any `to_date` after V2's activation date.

### Proof of Concept

**Concrete numerical example** (from the codebase's own test):

- Node provider has 2 Type3 nodes in California (rate=10000/day, coeff=0.95) and 2 Type3.1 nodes in Nevada (rate=10000/day, coeff=0.80), all grouped under `North America:USA`.

| Algorithm | Calculation | Per-node avg reward |
|-----------|-------------|---------------------|
| V1 (buggy) | avg_rate=10000, avg_coeff=0.875 → `10000 + 8750 + 7656.25 + 6699.22 = 33105.47` | **8276.37** |
| V2 (correct) | sorted pairs → `10000 + 9500 + 9025 + 7220 = 35745` | **8936.25** |

V1 underpays by **~7.9%** in this scenario. The gap widens as rate/coefficient heterogeneity increases. [1](#0-0) [3](#0-2) [7](#0-6)

### Citations

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

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L492-494)
```rust
                    for (rate, coeff) in &entries {
                        total_rewards += rate * running_coeff;
                        running_coeff *= coeff;
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

**File:** rs/node_rewards/canister/src/canister/mod.rs (L181-202)
```rust
    fn calculate_rewards_for_date(
        &self,
        date: &NaiveDate,
        algorithm_version: Option<RewardsCalculationAlgorithmVersion>,
    ) -> Result<DailyResults, String> {
        // Default to currently used algorithm
        let rewards_calculator_version = algorithm_version.unwrap_or_default();

        match rewards_calculator_version.version {
            RewardsCalculationV1::VERSION => {
                RewardsCalculationV1::calculate_rewards_for_date(date, &self)
                    .map_err(|e| format!("Could not calculate rewards: {e:?}"))
            }
            RewardsCalculationV2::VERSION => {
                RewardsCalculationV2::calculate_rewards_for_date(date, &self)
                    .map_err(|e| format!("Could not calculate rewards: {e:?}"))
            }
            _ => Err(format!(
                "Rewards Calculation Version: {rewards_calculator_version:?} is not supported"
            )),
        }
    }
```

**File:** rs/node_rewards/canister/api/src/lib.rs (L18-24)
```rust
impl Default for RewardsCalculationAlgorithmVersion {
    fn default() -> Self {
        Self {
            version: RewardsCalculationV2::VERSION,
        }
    }
}
```

**File:** rs/nns/governance/src/governance.rs (L7664-7666)
```rust
        let (rewards_per_node_provider, algorithm_version) = self
            .get_node_providers_xdr_permyriad_rewards(start_date, end_date)
            .await?;
```
