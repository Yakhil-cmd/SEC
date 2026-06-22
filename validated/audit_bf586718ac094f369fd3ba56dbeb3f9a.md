### Title
Precision Loss via `f64` Truncation in Type3 Node Reward Calculation - (`File: rs/registry/node_provider_rewards/src/lib.rs`)

### Summary

The `calculate_rewards_v0` function in `rs/registry/node_provider_rewards/src/lib.rs` computes per-node rewards for type3 nodes using floating-point arithmetic and then truncates the result to `u64` on every loop iteration. This per-iteration truncation compounds across many nodes, causing node providers to systematically receive less ICP than they are entitled to. The analog to the Ajna bug is exact: a floating-point intermediate is truncated (effectively a floor-division) before being accumulated, rather than accumulating in full precision and truncating only once at the end.

### Finding Description

In `calculate_rewards_v0`, for every type3 or type3.1 node, the per-node XDR reward is computed as:

```rust
// rs/registry/node_provider_rewards/src/lib.rs, line 128
let node_reward = (reward_base * np_coeff) as u64;
```

`reward_base` is `rate.xdr_permyriad_per_node_per_month as f64` and `np_coeff` is a running `f64` coefficient that decays geometrically (e.g. `0.80^n`). The cast `as u64` truncates the fractional part on every single node in the loop. The truncated integer is then accumulated into `dc_reward`, and the decayed `np_coeff` is carried forward for the next node.

Because the truncation happens inside the loop (line 128) rather than once after the full sum is computed, each node loses up to ~1 XDR permyriad unit of reward per iteration. For a node provider with many type3 nodes (the protocol supports arbitrarily many), the cumulative loss is `O(node_count)` XDR permyriad units per reward period.

The downstream path is:
1. `calculate_rewards_v0` → `np_rewards` (u64 XDR permyriad total)
2. `get_node_provider_reward` in `rs/nns/governance/src/governance.rs` line 8254 converts this to ICP e8s: `(xdr_permyriad_reward * TOKEN_SUBDIVIDABLE_BY) / xdr_permyriad_per_icp`
3. The governance canister mints and transfers the ICP to the node provider's reward account.

The truncation error is therefore directly reflected in the final minted ICP amount.

### Impact Explanation

Node providers operating many type3 nodes (the most common current node type) receive systematically less ICP than the reward table entitles them to. The shortfall is proportional to the number of nodes: with `N` type3 nodes, up to `N` XDR permyriad units are lost per reward period. At a typical XDR/ICP rate of ~155,000 permyriad/ICP and `TOKEN_SUBDIVIDABLE_BY = 1e8`, each lost permyriad unit corresponds to ~645 e8s (~0.00000645 ICP). For a node provider with 100 type3 nodes, this is ~645,000 e8s (~0.00645 ICP) per monthly reward period — a small but real and systematic underpayment that accumulates over years of operation. The lost funds are not redistributed; they simply vanish from the reward calculation.

### Likelihood Explanation

This code path is executed every time the NNS governance canister calls `get_monthly_node_provider_rewards` or `get_node_providers_rewards`, which happens on a monthly cadence triggered by a governance proposal or automated timer. Every node provider with more than one type3 node is affected on every reward cycle. The condition is always true for the current IC mainnet topology, which has many node providers with multiple type3 nodes.

### Recommendation

Accumulate the reward in `f64` (or use `rust_decimal::Decimal` as the performance-based algorithm already does) across the entire loop, and perform the single `as u64` truncation only once after the loop completes:

```rust
let mut dc_reward_f64: f64 = 0.0;
for i in 0..*node_count {
    let node_reward_f64 = reward_base * np_coeff;
    // ... logging with node_reward_f64 as u64 for display only
    dc_reward_f64 += node_reward_f64;
    np_coeff *= dc_reward_coefficient_percent;
}
let dc_reward = dc_reward_f64 as u64;
```

Alternatively, migrate the entire type3 calculation to `rust_decimal::Decimal` as the performance-based algorithm (`rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs`) already does correctly.

### Proof of Concept

**Vulnerable code** in `rs/registry/node_provider_rewards/src/lib.rs`:

```rust
let mut dc_reward = 0;
for i in 0..*node_count {
    let node_reward = (reward_base * np_coeff) as u64;  // truncation here, every iteration
    dc_reward += node_reward;
    np_coeff *= dc_reward_coefficient_percent;
}
``` [1](#0-0) 

**Concrete example:**

Suppose `reward_base = 22_000_000.0` (22M XDR permyriad/month) and `dc_reward_coefficient_percent = 0.70` (70% decay). For 3 nodes:

| Node | `reward_base * np_coeff` (exact f64) | `as u64` (truncated) | Lost |
|------|--------------------------------------|----------------------|------|
| 0 | 22,000,000.0 | 22,000,000 | 0 |
| 1 | 15,400,000.0 | 15,400,000 | 0 |
| 2 | 10,780,000.0 | 10,780,000 | 0 |

With a coefficient like `0.97` and a base of `22_000_001`:

| Node | exact | truncated | lost |
|------|-------|-----------|------|
| 0 | 22,000,001.0 | 22,000,001 | 0 |
| 1 | 21,340,000.97 | 21,340,000 | 0.97 |
| 2 | 20,699,800.9409 | 20,699,800 | 0.94 |

The test in `rs/registry/canister/src/get_node_providers_monthly_xdr_rewards.rs` confirms the pattern — the expected reward for NP2 with 14 type3 nodes is computed using the same `node_reward_ch as u64` truncation per iteration, meaning the test itself encodes the lossy behavior as the expected value rather than catching it. [2](#0-1) 

The `get_node_provider_reward` function in governance then converts the already-truncated XDR total to ICP e8s: [3](#0-2) 

The correct approach — multiply first, divide once — is already used in the performance-based algorithm: [4](#0-3)

### Citations

**File:** rs/registry/node_provider_rewards/src/lib.rs (L101-138)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L8248-8255)
```rust
pub fn get_node_provider_reward(
    np: &NodeProvider,
    xdr_permyriad_reward: u64,
    xdr_permyriad_per_icp: u64,
) -> Option<RewardNodeProvider> {
    if let Some(np_id) = np.id.as_ref() {
        let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
            / xdr_permyriad_per_icp as u128) as u64;
```

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L485-501)
```rust
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
```
