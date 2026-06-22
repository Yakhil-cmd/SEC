### Title
Unchecked Division by Zero in Node Provider Reward Calculation - (File: rs/nns/governance/src/governance.rs)

### Summary
The `get_node_provider_reward` function in the NNS governance canister performs integer division by `xdr_permyriad_per_icp` without a zero guard. If this value reaches zero — possible when `minimum_icp_xdr_rate` is set to zero via a governance proposal and the CMC canister has no stored rates — the governance canister traps with an integer division-by-zero panic, disrupting node provider reward computation.

### Finding Description
`get_node_provider_reward` computes the ICP reward amount as:

```rust
let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
    / xdr_permyriad_per_icp as u128) as u64;
``` [1](#0-0) 

There is no check that `xdr_permyriad_per_icp != 0` before the division. In Rust, integer division by zero panics unconditionally, which in a canister context causes a trap.

The callers compute the divisor as:

```rust
let xdr_permyriad_per_icp = max(avg_xdr_permyriad_per_icp, minimum_xdr_permyriad_per_icp);
``` [2](#0-1) 

where `minimum_xdr_permyriad_per_icp` is derived from `NetworkEconomics::minimum_icp_xdr_rate`:

```rust
let minimum_xdr_permyriad_per_icp = self
    .economics()
    .minimum_icp_xdr_rate
    .saturating_mul(NetworkEconomics::ICP_XDR_RATE_TO_BASIS_POINT_MULTIPLIER);
``` [3](#0-2) 

If `minimum_icp_xdr_rate` is set to `0` (via a governance proposal) and the CMC canister returns `0` for the average rate (e.g., fresh/uninitialized state with no stored rates), then `max(0, 0) = 0` is passed as the divisor, triggering a panic. The same pattern exists in `get_monthly_node_provider_rewards`: [4](#0-3) 

By contrast, the newer Mission 70 maturity modulation code in the governance canister explicitly guards against this:

```rust
if reference_icp_price == 0 {
    return Err("reference price averaged to zero".to_string());
}
``` [5](#0-4) 

The CMC canister itself rejects zero rates at ingestion:

```rust
if proposed_conversion_rate.xdr_permyriad_per_icp == 0 {
    return Err("Proposed conversion rate must be greater than 0".to_string());
}
``` [6](#0-5) 

However, `get_node_provider_reward` has no such guard and relies entirely on callers to prevent zero.

### Impact Explanation
A panic inside `get_node_provider_reward` causes the NNS governance canister to trap during its periodic node provider reward computation. This halts the reward minting cycle for all node providers until the state is corrected. Because the governance canister is a system canister on the NNS subnet, repeated traps in periodic tasks can stall reward distribution indefinitely.

### Likelihood Explanation
Likelihood is low but non-zero. It requires two concurrent conditions: (1) a governance proposal that sets `minimum_icp_xdr_rate` to `0` (which is not validated against zero in the NetworkEconomics update path), and (2) the CMC returning `0` for the 30-day average (possible on a fresh deployment or if all stored rates are evicted). Neither condition alone is sufficient, but both are reachable without a malicious governance majority — an accidental or misconfigured proposal suffices.

### Recommendation
Add an explicit zero guard inside `get_node_provider_reward` before the division:

```rust
if xdr_permyriad_per_icp == 0 {
    return None; // or log and skip
}
let amount_e8s = ((xdr_permyriad_reward as u128 * TOKEN_SUBDIVIDABLE_BY as u128)
    / xdr_permyriad_per_icp as u128) as u64;
```

Additionally, validate that `minimum_icp_xdr_rate` cannot be set to zero in the NetworkEconomics proposal validation path, mirroring the CMC's own enforcement.

### Proof of Concept

1. Submit a governance proposal setting `NetworkEconomics::minimum_icp_xdr_rate = 0`.
2. Ensure the CMC canister has no stored ICP/XDR rates (fresh state or all rates expired), so `get_average_icp_xdr_conversion_rate` returns `xdr_permyriad_per_icp = 0`.
3. Wait for the governance canister's periodic task to invoke `get_node_providers_rewards` or `get_monthly_node_provider_rewards`.
4. `xdr_permyriad_per_icp = max(0, 0) = 0` is passed to `get_node_provider_reward`.
5. The line `/ xdr_permyriad_per_icp as u128` panics with integer division by zero, trapping the canister call and halting node provider reward minting. [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L7673-7676)
```rust
        let minimum_xdr_permyriad_per_icp = self
            .economics()
            .minimum_icp_xdr_rate
            .saturating_mul(NetworkEconomics::ICP_XDR_RATE_TO_BASIS_POINT_MULTIPLIER);
```

**File:** rs/nns/governance/src/governance.rs (L7680-7680)
```rust
        let xdr_permyriad_per_icp = max(avg_xdr_permyriad_per_icp, minimum_xdr_permyriad_per_icp);
```

**File:** rs/nns/governance/src/governance.rs (L7749-7758)
```rust
        let xdr_permyriad_per_icp = max(avg_xdr_permyriad_per_icp, minimum_xdr_permyriad_per_icp);

        // Iterate over all node providers, calculate their rewards, and append them to
        // `rewards`
        for np in &self.heap_data.node_providers {
            if let Some(np_id) = &np.id {
                let xdr_permyriad_reward = *reg_rewards.get(np_id).unwrap_or(&0);

                if let Some(reward_node_provider) =
                    get_node_provider_reward(np, xdr_permyriad_reward, xdr_permyriad_per_icp)
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

**File:** rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs (L149-151)
```rust
    if reference_icp_price == 0 {
        return Err("reference price averaged to zero".to_string());
    }
```

**File:** rs/nns/cmc/src/main.rs (L1018-1020)
```rust
    if proposed_conversion_rate.xdr_permyriad_per_icp == 0 {
        return Err("Proposed conversion rate must be greater than 0".to_string());
    }
```
