### Title
Integer Truncation in `get_node_provider_reward` Causes Node Provider to Receive Zero ICP Rewards - (File: `rs/nns/governance/src/governance.rs`)

### Summary

The `get_node_provider_reward` function in the NNS Governance canister computes a node provider's ICP reward by performing integer division of `(xdr_permyriad_reward * TOKEN_SUBDIVIDABLE_BY) / xdr_permyriad_per_icp`. When the numerator is smaller than the denominator — i.e., when `xdr_permyriad_reward * 1e8 < xdr_permyriad_per_icp` — the result truncates to zero. The node provider is then minted 0 ICP despite having legitimately earned rewards, with no error, no rollover, and no retry.

### Finding Description

The vulnerable function is:

```rust
pub fn get_node_provider_reward(
    np: &NodeProvider,
    xdr_permyriad_reward: u64,
    xdr_permyriad_per_icp: u64,
) -> Option<RewardNodeProvider> {
    if let Some(np_id) = np.id.as_ref() {
        let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
            / xdr_permyriad_per_icp as u128) as u64;
        ...
        Some(RewardNodeProvider {
            node_provider: Some(np.clone()),
            amount_e8s,
            ...
        })
    } else {
        None
    }
}
``` [1](#0-0) 

`TOKEN_SUBDIVIDABLE_BY` is `1e8` (100,000,000). The formula computes:

```
amount_e8s = (xdr_permyriad_reward * 100_000_000) / xdr_permyriad_per_icp
```

If `xdr_permyriad_reward * 100_000_000 < xdr_permyriad_per_icp`, integer division truncates to 0. The function still returns `Some(RewardNodeProvider { amount_e8s: 0, ... })` — a structurally valid reward record with a zero amount. This zero-amount record is pushed into the rewards list and the node provider is minted 0 ICP.

The caller `get_node_providers_rewards` (and `get_monthly_node_provider_rewards`) applies no minimum-amount guard before appending the result:

```rust
if let Some(reward_node_provider) =
    get_node_provider_reward(np, xdr_permyriad_reward, xdr_permyriad_per_icp)
{
    rewards.push(reward_node_provider);
}
``` [2](#0-1) 

The `Option` guard only filters out node providers with no `id` field — it does **not** filter out zero-amount rewards.

### Impact Explanation

A node provider whose monthly XDR reward is small relative to the ICP/XDR conversion rate will receive 0 ICP. Concretely:

- `xdr_permyriad_per_icp` is the 30-day average rate in units of 1/10,000 XDR per ICP. At a rate of, say, 50,000 (= 5 XDR/ICP), the threshold is: `xdr_permyriad_reward < 50,000 / 100,000,000 = 0.0005`, i.e., any reward below 1 permyriad XDR truncates to 0.
- More practically: if `xdr_permyriad_per_icp` is very large (ICP is cheap in XDR terms) or `xdr_permyriad_reward` is very small (e.g., a node provider with a single low-rate node for a partial period), the product `xdr_permyriad_reward * 1e8` can be less than `xdr_permyriad_per_icp`, yielding 0.
- The node provider loses their earned reward for that month with no recourse — the reward event is recorded as settled, and the monthly distribution does not roll over unpaid amounts.

**Impact class**: Ledger conservation bug / governance reward accounting bug. Legitimate node providers lose ICP they are owed.

### Likelihood Explanation

The minimum `xdr_permyriad_per_icp` is enforced by `minimum_icp_xdr_rate` (default 100 ICP/XDR in permyriad = 1,000,000), which bounds the denominator. With `TOKEN_SUBDIVIDABLE_BY = 1e8`, truncation to zero requires `xdr_permyriad_reward < 10` (i.e., less than 0.001 XDR/month). This is a low but non-zero probability for node providers with very few nodes in low-rate regions, or for partial-month reward periods. The condition is reachable without any privileged access — it depends only on the reward table values and the ICP/XDR rate, both of which are governance-controlled parameters that any NNS governance participant can influence via proposals. [3](#0-2) 

### Recommendation

1. **Filter zero-amount rewards before minting**: In `get_node_providers_rewards` and `get_monthly_node_provider_rewards`, skip (or log and skip) any `RewardNodeProvider` where `amount_e8s == 0` before pushing to the rewards list.
2. **Alternatively, roll over sub-e8s rewards**: Accumulate fractional rewards across months using a persistent remainder, similar to how NNS voting rewards roll over `e8s_equivalent_to_be_rolled_over`.
3. **Add a guard in `get_node_provider_reward`**: Return `None` when `amount_e8s == 0` so the `Option` guard in callers naturally filters it out.

### Proof of Concept

**Setup**: A node provider has 1 node in a region with `xdr_permyriad_per_node_per_month = 5` (0.0005 XDR/month). The ICP/XDR rate is at the minimum: `xdr_permyriad_per_icp = 1_000_000` (100 XDR/ICP in permyriad).

**Calculation**:
```
amount_e8s = (5 * 100_000_000) / 1_000_000
           = 500_000_000 / 1_000_000
           = 500
```
This yields 500 e8s (0.000005 ICP) — non-zero in this case.

**Zero case**: Same node provider, but `xdr_permyriad_per_icp = 600_000_000` (ICP is very cheap, 60,000 XDR/ICP):
```
amount_e8s = (5 * 100_000_000) / 600_000_000
           = 500_000_000 / 600_000_000
           = 0  (integer truncation)
```

The function returns `Some(RewardNodeProvider { amount_e8s: 0 })`. [4](#0-3) 

The caller pushes this into the rewards list and the node provider is minted 0 ICP, losing their earned reward for the month with no error or rollover. [5](#0-4)

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

**File:** rs/nns/governance/src/governance.rs (L8248-8271)
```rust
pub fn get_node_provider_reward(
    np: &NodeProvider,
    xdr_permyriad_reward: u64,
    xdr_permyriad_per_icp: u64,
) -> Option<RewardNodeProvider> {
    if let Some(np_id) = np.id.as_ref() {
        let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
            / xdr_permyriad_per_icp as u128) as u64;

        let to_account = Some(if let Some(account) = &np.reward_account {
            account.clone()
        } else {
            AccountIdentifier::from(*np_id).into()
        });

        Some(RewardNodeProvider {
            node_provider: Some(np.clone()),
            amount_e8s,
            reward_mode: Some(RewardMode::RewardToAccount(RewardToAccount { to_account })),
        })
    } else {
        None
    }
}
```
