Based on my research, I found a valid analog in the NNS governance canister. Let me present the finding.

### Title
Node Provider Existence Checked at Proposal Execution Time, Not Creation Time - (File: `rs/nns/governance/src/governance.rs`)

### Summary
The `reward_node_provider_helper` function in NNS governance checks whether a node provider is still registered at proposal **execution** time. If a node provider is removed via a separate governance proposal between when a `RewardNodeProvider` proposal is created and when it executes, the reward permanently fails and the node provider never receives their earned ICP.

### Finding Description
In `reward_node_provider_helper` at lines 3938–3947 of `rs/nns/governance/src/governance.rs`, the function checks `self.heap_data.node_providers.iter().any(|np| np.id == node_provider.id)` at execution time:

```rust
if !self
    .heap_data
    .node_providers
    .iter()
    .any(|np| np.id == node_provider.id)
{
    Err(GovernanceError::new_with_message(
        ErrorType::NotFound,
        format!("Node provider with id {np_principal} not found."),
    ))
```

A `RewardNodeProvider` proposal (or a `RewardNodeProviders` proposal with `use_registry_derived_rewards = false`) specifies a node provider at **creation** time and then goes through a multi-day voting period. During this window, a separate `AddOrRemoveNodeProvider` proposal can remove the node provider. When the reward proposal subsequently executes, the node provider is no longer found, and the function returns `NotFound`. The proposal is then permanently marked as failed via `set_proposal_execution_status` at line 3983, with no retry mechanism.

The `reward_node_providers` function at line 3987 continues processing other rewards even when one fails, but the failed reward is permanently lost — the ICP is never minted. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation
The node provider performed work during the reward period and was entitled to ICP rewards. After removal, the reward proposal fails permanently. The ICP that should have been minted is never created, and there is no mechanism to recover or retry the reward for a removed node provider. This is a governance authorization bug where the entity status check occurs at the wrong lifecycle point — execution time rather than creation time. The node provider's earned ICP

### Citations

**File:** rs/nns/governance/src/governance.rs (L3932-3978)
```rust
    async fn reward_node_provider_helper(
        &mut self,
        reward: &RewardNodeProvider,
    ) -> Result<(), GovernanceError> {
        if let Some(node_provider) = &reward.node_provider {
            if let Some(np_principal) = &node_provider.id {
                if !self
                    .heap_data
                    .node_providers
                    .iter()
                    .any(|np| np.id == node_provider.id)
                {
                    Err(GovernanceError::new_with_message(
                        ErrorType::NotFound,
                        format!("Node provider with id {np_principal} not found."),
                    ))
                } else {
                    // Check that the amount to distribute is not above
                    // than the maximum set in network economics.
                    let maximum_node_provider_rewards_e8s =
                        self.economics().maximum_node_provider_rewards_e8s;
                    if reward.amount_e8s > maximum_node_provider_rewards_e8s {
                        Err(GovernanceError::new_with_message(
                            ErrorType::PreconditionFailed,
                            format!(
                                "Proposed reward {} greater than maximum {}",
                                reward.amount_e8s, maximum_node_provider_rewards_e8s
                            ),
                        ))
                    } else {
                        self.mint_reward_to_neuron_or_account(np_principal, reward)
                            .await
                    }
                }
            } else {
                Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    "Node provider has no ID.",
                ))
            }
        } else {
            Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Proposal was missing the node provider.",
            ))
        }
    }
```

**File:** rs/nns/governance/src/governance.rs (L3980-3984)
```rust
    /// Rewards a node provider.
    async fn reward_node_provider(&mut self, pid: u64, reward: &RewardNodeProvider) {
        let result = self.reward_node_provider_helper(reward).await;
        self.set_proposal_execution_status::<()>(pid, result.map(|()| vec![]));
    }
```

**File:** rs/nns/governance/src/governance.rs (L3987-4006)
```rust
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
    }
```

**File:** rs/nns/governance/src/governance.rs (L4008-4021)
```rust
    /// Execute a RewardNodeProviders proposal
    async fn reward_node_providers_from_proposal(
        &mut self,
        pid: u64,
        reward_nps: RewardNodeProviders,
    ) {
        let result = if reward_nps.use_registry_derived_rewards == Some(true) {
            self.mint_monthly_node_provider_rewards().await
        } else {
            self.reward_node_providers(&reward_nps.rewards).await
        };

        self.set_proposal_execution_status::<()>(pid, result.map(|()| vec![]));
    }
```
