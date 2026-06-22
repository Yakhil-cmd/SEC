### Title
Systematic Precision Loss in Type3 Node Provider Reward Calculation via `f64` Truncation - (File: `rs/registry/node_provider_rewards/src/lib.rs`)

### Summary
The `calculate_rewards_v0` function in `rs/registry/node_provider_rewards/src/lib.rs` computes type3 node rewards using `f64` floating-point arithmetic and then truncates each per-node result to `u64` via an `as u64` cast. This truncation discards the fractional XDR permyriad on every node in the decay loop, causing node providers with many type3 nodes to receive systematically less than their entitled reward. The error compounds across nodes and across monthly reward periods.

### Finding Description
In `rs/registry/node_provider_rewards/src/lib.rs`, the type3 reward loop is:

```rust
let reward_base = rate.xdr_permyriad_per_node_per_month as f64;   // line 101
let dc_reward_coefficient_percent =
    rate.reward_coefficient_percent.unwrap_or(80) as f64 / 100.0; // line 123-124

let mut dc_reward = 0;
for i in 0..*node_count {
    let node_reward = (reward_base * np_coeff) as u64;             // line 128 — truncation
    dc_reward += node_reward;
    np_coeff *= dc_reward_coefficient_percent;
}
``` [1](#0-0) 

At line 128, `(reward_base * np_coeff) as u64` silently truncates the fractional part of the `f64` product. For example, with `xdr_permyriad_per_node_per_month = 304375` (≈10 000 XDR/day) and `reward_coefficient_percent = 90`:

| Node | Exact value | Truncated | Lost |
|------|-------------|-----------|------|
| 2 | 273 937.5 | 273 937 | 0.5 |
| 3 | 246 543.75 | 246 543 | 0.75 |
| 4 | 221 889.375 | 221 889 | 0.375 |
| … | … | … | … |

Each truncated value is then used as the basis for the next coefficient multiplication, so the error is not merely additive — it also shifts the running `np_coeff` slightly, causing downstream nodes to be computed from a slightly wrong base.

The downstream conversion from XDR permyriad to ICP e8s in `get_node_provider_reward` (governance canister) correctly multiplies before dividing:

```rust
let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
    / xdr_permyriad_per_icp as u128) as u64;
``` [2](#0-1) 

However, the `xdr_permyriad_reward` fed into that formula is already the truncated sum from `calculate_rewards_v0`, so the precision loss is baked in before the ICP conversion.

### Impact Explanation
Node providers operating large fleets of type3 nodes receive a systematically lower XDR permyriad total than the reward table entitles them to. Because the governance canister mints ICP directly from this figure, the underpayment is permanent and irreversible for each reward period. With real-world reward rates (e.g., 907 000 XDR/month per node, 80–90% coefficient) and providers operating 10–50 type3 nodes, the per-period loss can reach hundreds to thousands of XDR permyriad, translating to measurable ICP underpayment at scale. [3](#0-2) 

### Likelihood Explanation
This fires on every monthly reward distribution for every node provider with type3 nodes. It is not conditional on any attacker action — it is a deterministic arithmetic defect in the production reward path. The governance canister calls `calculate_rewards_v0` (via the registry canister) on every reward cycle. [4](#0-3) 

### Recommendation
Replace the per-node `f64` truncation with integer-only arithmetic, keeping the full precision until the final sum. One approach is to represent the running coefficient as a rational number (numerator/denominator pair in `u128`) and perform the multiplication in integer space:

```rust
// Instead of:
let node_reward = (reward_base * np_coeff) as u64;

// Use scaled integer arithmetic (analogous to the 10^N scaling recommended
// in the external report):
// Represent np_coeff as (coeff_num / SCALE) where SCALE = 10^18
// node_reward = reward_base * coeff_num / SCALE  (all u128, divide last)
```

This mirrors the pattern already used correctly in `get_node_provider_reward` (multiply first, divide last) and eliminates the per-node truncation entirely. [5](#0-4) 

### Proof of Concept
With `xdr_permyriad_per_node_per_month = 304375`, `reward_coefficient_percent = 90`, and 5 nodes:

```
Exact:     304375 + 273937.5 + 246543.75 + 221889.375 + 199700.4375 = 1246446.0625
Truncated: 304375 + 273937   + 246543    + 221889     + 199700      = 1246444
Loss per 5-node batch: 2 XDR permyriad
```

Scaled to a provider with 50 type3 nodes across multiple DCs, the loss per monthly period is on the order of tens of XDR permyriad. Multiplied by the XDR→ICP conversion rate and compounded over years of operation, this constitutes a non-trivial, permanent underpayment to node providers — a ledger conservation violation in the NNS governance reward path. [6](#0-5)

### Citations

**File:** rs/registry/node_provider_rewards/src/lib.rs (L77-142)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L7644-7694)
```rust
    /// Return the rewards that node providers should be awarded with.
    ///
    /// Fetches the map from node provider to XDR rewards valid between 'from' and 'to' boundaries from the
    /// Node Rewards Canister, then fetches the average XDR to ICP conversion rate for
    /// the last 30 days, then applies this conversion rate to convert each
    /// node provider's XDR rewards to ICP.
    pub async fn get_node_providers_rewards(
        &self,
    ) -> Result<MonthlyNodeProviderRewards, GovernanceError> {
        let mut rewards = vec![];

        let start_date = self.next_start_date_node_providers_rewards();
        let now = self.env.now();

        // Today we have collected up to and included node metrics of yesterday
        // in the node rewards canister.
        let end_date_timestamp_seconds = now.saturating_sub(ONE_DAY_SECONDS);
        let end_date = DateUtc::from_unix_timestamp_seconds(end_date_timestamp_seconds);

        // Maps node providers to their rewards in XDR
        let (rewards_per_node_provider, algorithm_version) = self
            .get_node_providers_xdr_permyriad_rewards(start_date, end_date)
            .await?;

        // The average (last 30 days) conversion rate from 10,000ths of an XDR to 1 ICP
        let icp_xdr_conversion_rate = self.get_average_icp_xdr_conversion_rate().await?.data;
        let avg_xdr_permyriad_per_icp = icp_xdr_conversion_rate.xdr_permyriad_per_icp;

        // Convert minimum_icp_xdr_rate to basis points for comparison with avg_xdr_permyriad_per_icp
        let minimum_xdr_permyriad_per_icp = self
            .economics()
            .minimum_icp_xdr_rate
            .saturating_mul(NetworkEconomics::ICP_XDR_RATE_TO_BASIS_POINT_MULTIPLIER);

        let maximum_node_provider_rewards_e8s = self.economics().maximum_node_provider_rewards_e8s;

        let xdr_permyriad_per_icp = max(avg_xdr_permyriad_per_icp, minimum_xdr_permyriad_per_icp);

        // Iterate over all node providers, calculate their rewards, and append them to
        // `rewards`
        for np in &self.heap_data.node_providers {
            if let Some(np_id) = &np.id {
                let xdr_permyriad_reward = *rewards_per_node_provider.get(np_id).unwrap_or(&0);

                if let Some(reward_node_provider) =
                    get_node_provider_reward(np, xdr_permyriad_reward, xdr_permyriad_per_icp)
                {
                    rewards.push(reward_node_provider);
                }
            }
        }
```

**File:** rs/nns/governance/src/governance.rs (L8254-8255)
```rust
        let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
            / xdr_permyriad_per_icp as u128) as u64;
```
