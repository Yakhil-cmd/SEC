### Title
Removed Node Provider Loses Earned Rewards for the Current Reward Period - (`rs/nns/governance/src/governance.rs`)

### Summary

Under the performance-based node provider reward system (now enabled on mainnet), when a node provider is removed via a governance proposal mid-period, they are immediately deleted from `heap_data.node_providers`. The subsequent monthly reward distribution iterates only over currently registered node providers, silently discarding the XDR rewards the Node Rewards Canister (NRC) computed for the removed provider's active days in the current period.

### Finding Description

The performance-based reward flow is:

1. `mint_monthly_node_provider_rewards()` calls `get_node_providers_rewards()` when `are_performance_based_rewards_enabled()` is `true`.
2. `get_node_providers_rewards()` computes `start_date` via `next_start_date_node_providers_rewards()` (the day after the previous period's `end_date`) and `end_date` as `now - ONE_DAY_SECONDS`.
3. It calls the NRC's `get_node_providers_rewards` endpoint with `[start_date, end_date]`. The NRC iterates every day in that range and sums per-provider XDR rewards based on node metrics — including metrics for nodes belonging to a provider who was active during those days.
4. Back in governance, the result is distributed **only** by iterating `self.heap_data.node_providers`:

```rust
for np in &self.heap_data.node_providers {
    if let Some(np_id) = &np.id {
        let xdr_permyriad_reward = *rewards_per_node_provider.get(np_id).unwrap_or(&0);
        ...
    }
}
```

When a node provider is removed via `ValidRemoveNodeProvider::execute()`, they are immediately deleted from `heap_data.node_providers`:

```rust
pub fn execute(&self, node_providers: &mut Vec<NodeProvider>) -> Result<(), GovernanceError> {
    let existing_node_provider_position =
        self.find_existing_node_provider_position(node_providers)?;
    node_providers.remove(existing_node_provider_position);
    Ok(())
}
```

The NRC will still return non-zero XDR rewards for the removed provider (their nodes were active during the period), but the governance loop never reaches their entry in `rewards_per_node_provider`. Those rewards are silently dropped — neither paid out nor rolled over.

### Impact Explanation

A node provider who operated nodes for part of a reward period (e.g., 20 of 30 days) and was then removed via a governance proposal receives **zero ICP** for that entire period. The earned rewards are permanently lost — they are not redistributed to other providers or rolled over. This is a direct loss of ICP compensation that the node provider legitimately earned by running IC infrastructure.

### Likelihood Explanation

`ARE_PERFORMANCE_BASED_REWARDS_ENABLED` defaults to `true` in production, so this code path is active on mainnet. Node provider removal is a routine governance action (NNS proposal type `AddOrRemoveNodeProvider` with `ToRemove`). Any such removal that occurs before the monthly reward distribution triggers the loss. The monthly reward period is approximately 30 days, so a removal at any point during the period causes the provider to forfeit up to a full month of ICP rewards.

### Recommendation

In `get_node_providers_rewards()`, after receiving `rewards_per_node_provider` from the NRC, distribute rewards to **all principals that appear in the NRC response**, not only those currently in `heap_data.node_providers`. Alternatively, record the set of node providers at the start of each reward period (in `MonthlyNodeProviderRewards.node_providers`, which is already stored) and use that snapshot for distribution rather than the live list. This mirrors the fix applied to the Olas `Dispenser.sol` analog: ensure the reward distribution boundary includes the removal period.

### Proof of Concept

**Step 1:** Node provider NP-X is active from day 1 of a reward period. The NRC accumulates daily metrics for NP-X's nodes.

**Step 2:** On day 20 of the 30-day period, an NNS governance proposal `AddOrRemoveNodeProvider { ToRemove: NP-X }` is adopted and executed. `ValidRemoveNodeProvider::execute()` removes NP-X from `heap_data.node_providers`. [1](#0-0) 

**Step 3:** On day 31, `mint_monthly_node_provider_rewards()` fires. Since `are_performance_based_rewards_enabled()` is `true`, it calls `get_node_providers_rewards()`. [2](#0-1) 

**Step 4:** `get_node_providers_rewards()` queries the NRC for `[start_date, day_30]`. The NRC returns `rewards_per_node_provider` containing NP-X's 20-day XDR total. [3](#0-2) 

**Step 5:** The distribution loop iterates only `self.heap_data.node_providers`. NP-X is absent. Their entry in `rewards_per_node_provider` is never accessed. The rewards are lost. [4](#0-3) 

The `are_performance_based_rewards_enabled()` flag is `true` by default in production: [5](#0-4)

### Citations

**File:** rs/nns/governance/src/proposals/add_or_remove_node_provider.rs (L207-211)
```rust
    pub fn execute(&self, node_providers: &mut Vec<NodeProvider>) -> Result<(), GovernanceError> {
        let existing_node_provider_position =
            self.find_existing_node_provider_position(node_providers)?;
        node_providers.remove(existing_node_provider_position);
        Ok(())
```

**File:** rs/nns/governance/src/governance.rs (L4067-4071)
```rust
        let monthly_node_provider_rewards = if are_performance_based_rewards_enabled() {
            self.get_node_providers_rewards().await?
        } else {
            self.get_monthly_node_provider_rewards().await?
        };
```

**File:** rs/nns/governance/src/governance.rs (L7664-7666)
```rust
        let (rewards_per_node_provider, algorithm_version) = self
            .get_node_providers_xdr_permyriad_rewards(start_date, end_date)
            .await?;
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

**File:** rs/nns/governance/src/lib.rs (L233-239)
```rust
thread_local! {
    static ARE_PERFORMANCE_BASED_REWARDS_ENABLED: Cell<bool> = const { Cell::new(true) };
}

pub(crate) fn are_performance_based_rewards_enabled() -> bool {
    ARE_PERFORMANCE_BASED_REWARDS_ENABLED.get()
}
```
