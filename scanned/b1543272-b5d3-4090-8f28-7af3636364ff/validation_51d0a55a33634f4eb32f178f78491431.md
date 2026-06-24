### Title
Incorrect Reward Calculation When Type3 Nodes Span Multiple Data Centers With Different Coefficients in Same Country - (File: rs/registry/node_provider_rewards/src/lib.rs)

### Summary
In `calculate_rewards_v0()`, when a Node Provider (NP) has type3 nodes in multiple data centers within the same country, each with a different `reward_coefficient_percent`, the running decay coefficient accumulated from one data center is incorrectly applied to the base rate of a subsequent data center. This is structurally identical to the reported vulnerability: a factor belonging to one asset is applied to the aggregated/subsequent balance of a different asset, producing an incorrect total.

### Finding Description
In `rs/registry/node_provider_rewards/src/lib.rs`, the function `calculate_rewards_v0()` computes monthly XDR rewards for node providers. For type3 nodes, a shared running coefficient `np_coeff` is maintained per `(node_provider_id, continent, country)` key across all data centers in the same country: [1](#0-0) 

The `reward_base` is fetched per-DC from the rewards table (line 101), and `dc_reward_coefficient_percent` is also fetched per-DC (line 123-124). However, `np_coeff` is a shared running value that carries over between DCs. When DC1 is processed first, `np_coeff` is reduced by DC1's coefficient. When DC2 is then processed, DC2's `reward_base` is multiplied by `np_coeff` — which already encodes DC1's coefficient — before DC2's own coefficient is applied.

**Concrete scenario** (two DCs in the same country, different coefficients):

| DC | Rate (XDR/node/month) | Coeff | Nodes |
|---|---|---|---|
| DC1 (`North America,US,California`) | 1,000 | 70% | 2 |
| DC2 (`North America,US,New York`) | 2,000 | 90% | 1 |

Processing order is lexicographic over node operator record keys (line 30): [2](#0-1) 

If DC1 is processed first:
- DC1 node 0: `1000 × 1.0 = 1000`, `np_coeff → 0.70`
- DC1 node 1: `1000 × 0.70 = 700`, `np_coeff → 0.49`
- DC2 node 0: `2000 × 0.49 = 980` ← DC1's accumulated coefficient is applied to DC2's base rate

Total: **2,680 XDR**

If DC2 were processed first:
- DC2 node 0: `2000 × 1.0 = 2000`, `np_coeff → 0.90`
- DC1 node 0: `1000 × 0.90 = 900`, `np_coeff → 0.63`
- DC1 node 1: `1000 × 0.63 = 630`, `np_coeff → 0.441`

Total: **3,530 XDR**

The difference is **850 XDR/month** (~31%) for this simple scenario. The code itself acknowledges this as a "known issue" in comments at lines 85–99: [3](#0-2) 

The root cause is identical to the reported vulnerability: a factor (coefficient) belonging to one asset (DC1) is applied to the base value of a different asset (DC2), because the algorithm operates on aggregated/sequential state rather than per-asset factors.

The same structural flaw exists in the performance-based algorithm V1 path in `rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs`, where rates and coefficients from different DCs are averaged before applying the decay sequence — a single averaged coefficient is applied to an averaged rate, rather than each DC's specific coefficient to its specific rate: [4](#0-3) 

The V2 algorithm explicitly fixes this by sorting and applying each entry's specific coefficient sequentially: [5](#0-4) 

### Impact Explanation
Node providers with type3 nodes in multiple data centers within the same country, where those DCs have different `reward_coefficient_percent` values, receive incorrect monthly XDR rewards. The magnitude of the error scales with the number of nodes and the difference between coefficients. A node provider may receive significantly less (or more) than the protocol intends. Since rewards are minted as ICP and transferred to node provider accounts, this constitutes a ledger conservation bug: the total ICP minted for node rewards does not match the protocol-specified reward schedule.

### Likelihood Explanation
The IC rewards table contains multiple regions within the same country with different `reward_coefficient_percent` values (e.g., `North America,US,California` vs. `North America,US,New York`). Any node provider operating type3 nodes across such regions is affected. The lexicographic ordering of node operator principal IDs is not controlled by the node provider, but the incorrect calculation occurs regardless of ordering whenever different coefficients are present — the ordering only determines the direction of the error (over- or under-payment). This is a realistic, non-hypothetical scenario given the current rewards table structure.

### Recommendation
For `calculate_rewards_v0()`: Rather than applying the running coefficient from one DC to the base rate of the next DC, the algorithm should either (a) compute each DC's reward independently using its own coefficient sequence starting from the current `np_coeff`, then advance `np_coeff` by the correct amount, or (b) adopt the V2 approach of collecting all (rate, coefficient) pairs for the country, sorting them, and applying the decay sequence in a single pass — matching each node's coefficient to its own rate before advancing the running coefficient.

For the V1 performance-based algorithm: Replace the averaging of rates and coefficients with the V2 approach (sort by rate desc, coeff desc; apply each entry's own coefficient sequentially).

### Proof of Concept
Using the scenario above with two node operators for the same NP, both in `North America,US`:

```
Node Operator A (key: "aaa...") → DC1 in "North America,US,California"
  type3: 2 nodes, rate=1000, coeff=70%

Node Operator B (key: "zzz...") → DC2 in "North America,US,New York"
  type3: 1 node, rate=2000, coeff=90%
```

Because `"aaa..." < "zzz..."` lexicographically, DC1 is processed first. The running coefficient after DC1's 2 nodes is `0.70^2 = 0.49`. DC2's node then receives `2000 × 0.49 = 980` instead of `2000 × 1.0 = 2000` (if DC2 were first) or the protocol-correct value. The node provider receives 2,680 XDR instead of 3,530 XDR — a 24% underpayment — with no mechanism for correction or appeal, since the calculation is deterministic and on-chain. [6](#0-5)

### Citations

**File:** rs/registry/node_provider_rewards/src/lib.rs (L30-30)
```rust
    for (key_string, node_operator) in node_operators.iter() {
```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L77-143)
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
            };
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
