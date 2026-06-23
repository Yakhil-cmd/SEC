### Title
Integer Division Truncation Yields Zero Node Provider ICP Reward for Small XDR Amounts - (`File: rs/nns/governance/src/governance.rs`)

### Summary
The `get_node_provider_reward` function in NNS governance converts a node provider's XDR reward to ICP e8s using integer division. When the XDR reward amount (`xdr_permyriad_reward`) is smaller than the ICP/XDR conversion rate (`xdr_permyriad_per_icp`), the numerator `xdr_permyriad_reward * TOKEN_SUBDIVIDABLE_BY` can still be less than `xdr_permyriad_per_icp`, causing the integer division to truncate to zero. This silently mints zero ICP to the node provider instead of the correct fractional amount.

### Finding Description

In `get_node_provider_reward`, the ICP e8s amount is computed as:

```rust
let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
    / xdr_permyriad_per_icp as u128) as u64;
```

where `TOKEN_SUBDIVIDABLE_BY = 100_000_000` (1e8). [1](#0-0) 

The formula is: `amount_e8s = (xdr_permyriad_reward * 1e8) / xdr_permyriad_per_icp`.

Integer division truncates toward zero. If `xdr_permyriad_reward * 1e8 < xdr_permyriad_per_icp`, the result is zero.

**Concrete scenario:** The `xdr_permyriad_per_icp` rate is expressed in units of 1/10,000 XDR per ICP. A realistic ICP price of ~5 XDR means `xdr_permyriad_per_icp ≈ 50,000`. The minimum enforced rate is `minimum_icp_xdr_rate * ICP_XDR_RATE_TO_BASIS_POINT_MULTIPLIER`, which is applied as a floor. [2](#0-1) 

For a node provider with a very small XDR reward (e.g., a single node in a region with a low rate, or a short reward period), if `xdr_permyriad_reward < xdr_permyriad_per_icp / 1e8`, the computed `amount_e8s` is zero. For example, with `xdr_permyriad_per_icp = 50,000` (5 XDR/ICP), any `xdr_permyriad_reward < 1` (i.e., reward < 0.0001 XDR) yields zero. More practically, with the performance-based algorithm accumulating daily rewards in `total_adjusted_rewards_xdr_permyriad` via `trunc()` before the XDR→ICP conversion, small daily reward accumulations can be truncated to zero at the XDR level before even reaching `get_node_provider_reward`. [3](#0-2) 

The `get_node_provider_reward` function is called from both `get_monthly_node_provider_rewards` and `get_node_providers_rewards` (the performance-based path), both of which pass the truncated `xdr_permyriad_reward` directly. [4](#0-3) [5](#0-4) 

The function always returns `Some(RewardNodeProvider { amount_e8s: 0, ... })` — it does not check for zero and does not return `None`. The zero-amount reward is then minted (or attempted to be minted) to the node provider's account, silently delivering nothing.

### Impact Explanation

A node provider whose computed XDR reward is small enough to truncate to zero ICP e8s receives **no ICP payment** for that reward period, despite having legitimately operated nodes. The reward is permanently lost — there is no rollover mechanism for node provider rewards. This is a **ledger conservation bug**: ICP that should be minted to node providers is silently discarded. The impact is proportional to how small the reward is relative to the ICP/XDR rate, and is most acute for:
- Node providers with few nodes in low-rate regions
- Short reward periods (performance-based daily accumulation)
- High ICP price (high `xdr_permyriad_per_icp`)

### Likelihood Explanation

The performance-based reward algorithm (`get_node_providers_rewards`) accumulates daily XDR rewards per provider and truncates them to `u64` before passing to `get_node_provider_reward`. [3](#0-2) 

For a node provider with a single node at a low regional rate (e.g., 1 XDR/month = ~33 XDR/day in permyriad), and a high ICP price (e.g., 20 XDR/ICP → `xdr_permyriad_per_icp = 200,000`), the daily reward in permyriad is approximately 33. Then `33 * 1e8 / 200,000 = 16,500 e8s` — non-zero. However, if the daily rate is below `xdr_permyriad_per_icp / 1e8 = 2`, any provider earning less than 2 permyriad XDR per day gets zero. This is a realistic edge case for providers with very few nodes or in low-rate regions. The minimum fallback rate of `xdr_permyriad_per_node_per_month: 1` is explicitly used when no rate is found in the table. [6](#0-5) 

### Recommendation

1. **Check for zero before minting**: In `get_node_provider_reward`, return `None` (or skip) when `amount_e8s == 0` to avoid emitting a zero-value reward record.
2. **Increase precision**: Multiply `xdr_permyriad_reward` by a larger precision factor (e.g., `1e16` instead of `1e8`) before dividing, then scale back — analogous to the BathBuddy fix. Alternatively, use `Decimal` arithmetic (as already used in the SNS reward path) to avoid truncation.
3. **Accumulate and roll over**: For the performance-based path, accumulate sub-threshold XDR rewards across periods rather than discarding them.

### Proof of Concept

Given:
- `xdr_permyriad_per_icp = 200_000` (ICP price = 20 XDR)
- `xdr_permyriad_reward = 1` (node provider earned 0.0001 XDR this period)
- `TOKEN_SUBDIVIDABLE_BY = 100_000_000`

Computation:
```
amount_e8s = (1 * 100_000_000) / 200_000 = 100_000_000 / 200_000 = 500
```
This case is fine. But with `xdr_permyriad_reward = 0` (after `trunc()` in the performance-based path when daily reward < 1 permyriad):
```
amount_e8s = (0 * 100_000_000) / 200_000 = 0
```

The node provider receives a `RewardNodeProvider { amount_e8s: 0 }` — a zero mint — and the reward is permanently lost. The fallback rate of `xdr_permyriad_per_node_per_month: 1` applied to a single node for a single day yields `1/30 ≈ 0.033` permyriad per day, which truncates to `0` in `total_adjusted_rewards_xdr_permyriad`, causing exactly this scenario. [7](#0-6) [3](#0-2) [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L7672-7680)
```rust
        // Convert minimum_icp_xdr_rate to basis points for comparison with avg_xdr_permyriad_per_icp
        let minimum_xdr_permyriad_per_icp = self
            .economics()
            .minimum_icp_xdr_rate
            .saturating_mul(NetworkEconomics::ICP_XDR_RATE_TO_BASIS_POINT_MULTIPLIER);

        let maximum_node_provider_rewards_e8s = self.economics().maximum_node_provider_rewards_e8s;

        let xdr_permyriad_per_icp = max(avg_xdr_permyriad_per_icp, minimum_xdr_permyriad_per_icp);
```

**File:** rs/nns/governance/src/governance.rs (L7684-7694)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L7751-7763)
```rust
        // Iterate over all node providers, calculate their rewards, and append them to
        // `rewards`
        for np in &self.heap_data.node_providers {
            if let Some(np_id) = &np.id {
                let xdr_permyriad_reward = *reg_rewards.get(np_id).unwrap_or(&0);

                if let Some(reward_node_provider) =
                    get_node_provider_reward(np, xdr_permyriad_reward, xdr_permyriad_per_icp)
                {
                    rewards.push(reward_node_provider);
                }
            }
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

**File:** rs/node_rewards/rewards_calculation/src/performance_based_algorithm/mod.rs (L647-655)
```rust
        let total_base_rewards_xdr_permyriad = total_base_rewards_xdr_permyriad
            .trunc()
            .to_u64()
            .expect("failed to truncate node_adjusted_rewards_xdr_permyriad");

        let total_adjusted_rewards_xdr_permyriad = total_adjusted_rewards_xdr_permyriad
            .trunc()
            .to_u64()
            .expect("failed to truncate node_adjusted_rewards_xdr_permyriad");
```

**File:** rs/registry/node_provider_rewards/src/lib.rs (L70-74)
```rust
                    NodeRewardRate {
                        xdr_permyriad_per_node_per_month: 1,
                        reward_coefficient_percent: Some(100),
                    }
                }
```
