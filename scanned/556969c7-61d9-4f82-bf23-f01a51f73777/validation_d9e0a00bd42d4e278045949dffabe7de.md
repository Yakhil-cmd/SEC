### Title
Monthly Node Provider Reward Transfer Result Silently Discarded, Causing Permanent ICP Loss - (File: rs/nns/governance/src/governance.rs)

### Summary
In `mint_monthly_node_provider_rewards`, the result of `reward_node_providers` — which performs ICP ledger minting transfers to all node providers — is explicitly discarded with `let _ =`. Immediately after, `update_most_recent_monthly_node_provider_rewards` is unconditionally called, advancing the "last rewarded" timestamp regardless of whether any transfer succeeded. If the ICP ledger is temporarily unavailable during the monthly distribution window, all node provider rewards for that month are permanently lost and never retried.

### Finding Description
In `rs/nns/governance/src/governance.rs`, the function `mint_monthly_node_provider_rewards` contains:

```rust
let _ = self
    .reward_node_providers(&monthly_node_provider_rewards.rewards)
    .await;
self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);
``` [1](#0-0) 

`reward_node_providers` iterates over every node provider reward and calls `reward_node_provider_helper`, which in turn calls `self.ledger.transfer_funds(...)` — a real ICP ledger minting transfer. It returns `Result<(), GovernanceError>`. [2](#0-1) 

The `let _ =` pattern unconditionally discards this `Result`. Immediately after, `update_most_recent_monthly_node_provider_rewards` is called, which sets `heap_data.most_recent_monthly_node_provider_rewards` to the current timestamp. [3](#0-2) 

The guard that prevents double-distribution checks this timestamp:

```rust
fn is_time_to_mint_monthly_node_provider_rewards(&self) -> bool {
    match &self.heap_data.most_recent_monthly_node_provider_rewards {
        None => false,
        Some(recent_rewards) => {
            self.env.now().saturating_sub(recent_rewards.timestamp)
                >= NODE_PROVIDER_REWARD_PERIOD_SECONDS
        }
    }
}
``` [4](#0-3) 

Because the timestamp is always updated regardless of transfer success, a failed distribution is never retried. The ICP that should have been minted to node providers is permanently undelivered.

### Impact Explanation
This is a **ledger conservation bug**. ICP tokens that the NNS governance canister is supposed to mint and distribute to node providers as monthly rewards can be permanently lost. The governance canister records the distribution as complete (via the timestamp update) even when zero or partial transfers succeeded. Node providers receive no compensation for that month, and the protocol has no mechanism to detect or recover from this state.

### Likelihood Explanation
The ICP ledger canister (`ryjl3-tyaaa-aaaaa-aaaba-cai`) can return `TemporarilyUnavailable` during canister upgrades, subnet maintenance, or under transient load. The monthly reward distribution is triggered automatically by the NNS heartbeat when `is_time_to_mint_monthly_node_provider_rewards()` returns true. No privileged access or attacker is required — a routine ledger upgrade coinciding with the heartbeat tick is sufficient to trigger the silent failure. The `reward_node_providers` function itself already logs individual failures: [5](#0-4) 

This confirms the authors anticipated transfer failures but did not propagate them to prevent the timestamp update.

### Recommendation
Remove the `let _ =` discard and propagate the error so that `update_most_recent_monthly_node_provider_rewards` is only called on full or partial success, or implement a retry queue for failed individual transfers similar to the SNS `disburse_maturity_in_progress` pattern. At minimum, the timestamp update must be conditional on the transfer result.

### Proof of Concept
1. NNS heartbeat fires; `is_time_to_mint_monthly_node_provider_rewards()` returns `true`.
2. `mint_monthly_node_provider_rewards` is called.
3. `reward_node_providers` calls `self.ledger.transfer_funds(...)` for each node provider; the ICP ledger returns `TemporarilyUnavailable` for all calls.
4. `reward_node_providers` returns `Err(GovernanceError { ... })`.
5. `let _ = ...` discards the error.
6. `update_most_recent_monthly_node_provider_rewards` sets `most_recent_monthly_node_provider_rewards.timestamp = now`.
7. `is_time_to_mint_monthly_node_provider_rewards()` now returns `false` for the next `NODE_PROVIDER_REWARD_PERIOD_SECONDS` (~30 days).
8. All node provider rewards for the month are permanently lost with no on-chain record of failure.

### Citations

**File:** rs/nns/governance/src/governance.rs (L3986-4005)
```rust
    /// Mint and transfer the specified Node Provider rewards
    async fn reward_node_providers(
        &mut self,
        rewards: &[RewardNodeProvider],
    ) -> Result<(), GovernanceError> {
        let mut result = Ok(());

        for reward in rewards {
            let reward_result = self.reward_node_provider_helper(reward).await;
            if reward_result.is_err() {
                println!(
                    "Rewarding {:?} failed. Reason: {:}",
                    reward,
                    reward_result.clone().unwrap_err()
                );
            }
            result = result.or(reward_result);
        }

        result
```

**File:** rs/nns/governance/src/governance.rs (L4025-4032)
```rust
    fn is_time_to_mint_monthly_node_provider_rewards(&self) -> bool {
        match &self.heap_data.most_recent_monthly_node_provider_rewards {
            None => false,
            Some(recent_rewards) => {
                self.env.now().saturating_sub(recent_rewards.timestamp)
                    >= NODE_PROVIDER_REWARD_PERIOD_SECONDS
            }
        }
```

**File:** rs/nns/governance/src/governance.rs (L4073-4076)
```rust
        let _ = self
            .reward_node_providers(&monthly_node_provider_rewards.rewards)
            .await;
        self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);
```
