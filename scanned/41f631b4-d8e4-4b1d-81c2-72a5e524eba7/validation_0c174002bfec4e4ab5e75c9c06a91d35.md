### Title
Silent Reward Loss via `Result::or` Logic Error in Batch Node Provider Payments - (`File: rs/nns/governance/src/governance.rs`)

### Summary

In `rs/nns/governance/src/governance.rs`, the `reward_node_providers` function iterates over a list of node provider rewards and uses `result = result.or(reward_result)` to accumulate the outcome. Due to how Rust's `Result::or` works — `Ok(v).or(res)` always returns `Ok(v)` regardless of `res` — if the **first** node provider's transfer succeeds, all subsequent transfer failures are silently swallowed. The function returns `Ok(())`, the `RewardNodeProviders` proposal is marked `Executed`, and the node providers whose transfers failed permanently lose their ICP rewards with no recourse.

### Finding Description

`reward_node_providers` is called in two paths:

1. **Explicit `RewardNodeProviders` proposal** (`use_registry_derived_rewards == Some(false)`): `reward_node_providers_from_proposal` calls `reward_node_providers` and passes its result directly to `set_proposal_execution_status`.
2. **Monthly automatic rewards** (`use_registry_derived_rewards == Some(true)`): `mint_monthly_node_provider_rewards` calls `reward_node_providers` but discards the result with `let _ = ...`, then unconditionally calls `update_most_recent_monthly_node_provider_rewards`, advancing the reward timestamp and permanently closing the reward window.

The defective accumulation logic in `reward_node_providers`:

```rust
let mut result = Ok(());
for reward in rewards {
    let reward_result = self.reward_node_provider_helper(reward).await;
    // ...
    result = result.or(reward_result);  // BUG: Ok(()).or(Err(e)) == Ok(())
}
result
```

Rust's `Result::or` semantics:
- `Ok(v).or(res)` → always `Ok(v)`, `res` is discarded
- `Err(e).or(res)` → `res`

So once `result` is `Ok(())` (after the first successful transfer), every subsequent `Err` is silently dropped. The function returns `Ok(())` even if N-1 of N transfers failed. [1](#0-0) 

The `reward_node_provider_helper` can fail for multiple realistic reasons before or during the ledger call:
- The node provider was removed from the registry between proposal adoption and execution
- The `amount_e8s` exceeds `maximum_node_provider_rewards_e8s` (if network economics changed)
- The ICP ledger is temporarily unavailable (e.g., during an upgrade) [2](#0-1) 

In the monthly path, the result is explicitly discarded: [3](#0-2) 

After `update_most_recent_monthly_node_provider_rewards` advances the timestamp, `is_time_to_mint_monthly_node_provider_rewards` will return `false` for the next ~30 days, permanently closing the reward window for the failed node providers. [4](#0-3) 

### Impact Explanation

**Ledger conservation bug / governance authorization bug.** Node providers who are entitled to ICP rewards permanently lose them:

- In the explicit-proposal path: the proposal is marked `Executed` (via `set_proposal_execution_status`), so it can never be re-executed. The failed node providers have no on-chain recourse.
- In the monthly path: the reward window is advanced unconditionally, so the failed transfers are never retried. [5](#0-4) [6](#0-5) 

### Likelihood Explanation

The trigger requires at least one node provider's transfer to succeed (first in iteration order) and at least one subsequent transfer to fail. Realistic triggers:

1. **Node provider deregistered between proposal adoption and execution** — the `reward_node_provider_helper` returns `Err(NotFound)` immediately, before any ledger call.
2. **ICP ledger temporarily unavailable** (e.g., during a canister upgrade) — the `transfer_funds` call returns an error for some but not all providers if the ledger recovers mid-loop.
3. **Network economics change** — `maximum_node_provider_rewards_e8s` is lowered between proposal creation and execution, causing some rewards to exceed the cap.

The NNS governance canister is a high-value target and `RewardNodeProviders` proposals are executed regularly (monthly). The ordering of node providers in `heap_data.node_providers` is deterministic, making the failure pattern predictable. [7](#0-6) 

### Recommendation

Replace the `result.or(reward_result)` accumulation with a pattern that preserves the first error while continuing to process all rewards:

```rust
async fn reward_node_providers(
    &mut self,
    rewards: &[RewardNodeProvider],
) -> Result<(), GovernanceError> {
    let mut first_error: Option<GovernanceError> = None;

    for reward in rewards {
        let reward_result = self.reward_node_provider_helper(reward).await;
        if let Err(e) = reward_result {
            println!("Rewarding {:?} failed. Reason: {:}", reward, e);
            if first_error.is_none() {
                first_error = Some(e);
            }
        }
    }

    match first_error {
        None => Ok(()),
        Some(e) => Err(e),
    }
}
```

Additionally, in `mint_monthly_node_provider_rewards`, the result of `reward_node_providers` should not be silently discarded. If any transfer fails, the monthly reward timestamp should **not** be advanced, or the failed providers should be recorded for retry.

### Proof of Concept

1. An NNS `RewardNodeProviders` proposal is submitted with two node providers: `[NP_A, NP_B]`.
2. The proposal is adopted by the NNS.
3. During execution, `NP_A`'s transfer succeeds → `result = Ok(())`.
4. `NP_B` was deregistered between adoption and execution → `reward_node_provider_helper` returns `Err(NotFound)`.
5. `result = Ok(()).or(Err(NotFound))` → `result = Ok(())`.
6. `set_proposal_execution_status` receives `Ok(())` → proposal is marked `Executed`.
7. `NP_B` receives no ICP. The proposal cannot be re-executed. `NP_B`'s reward is permanently lost. [8](#0-7) [5](#0-4)

### Citations

**File:** rs/nns/governance/src/governance.rs (L3162-3226)
```rust
    /// Records the outcome of executing a proposal. Decodes the raw reply
    /// bytes via `Reply::try_decode`, then updates `ProposalData` fields:
    ///
    /// On success:
    ///   - `success_value`: set to the decoded reply (if the reply type
    ///     produces one; `()` does not).
    ///   - `executed_timestamp_seconds`: set to now.
    ///
    /// On failure:
    ///   - `failure_reason`: set to the error (unless already executed).
    ///   - `failed_timestamp_seconds`: set to now (unless already executed).
    pub(crate) fn set_proposal_execution_status<Reply>(
        &mut self,
        proposal_id: u64,
        result: Result</* reply: */ Vec<u8>, GovernanceError>,
    ) where
        Reply: CallCanisterReply,
        SuccessfulProposalExecutionValue: From<Reply>,
    {
        let now_timestamp_seconds = self.env.now();

        // Fetch the ProposalData.
        let Some(proposal_data) = self.heap_data.proposals.get_mut(&proposal_id) else {
            // The proposal ID was not found. Something is wrong:
            // just log this information to aid debugging.
            println!(
                "{}Proposal {:?} not found when attempting to set execution result at {}.",
                LOG_PREFIX, proposal_id, now_timestamp_seconds,
            );
            return;
        };
        // The proposal has to be adopted before it is executed.
        debug_assert_eq!(proposal_data.status(), ProposalStatus::Adopted);

        // For later logging.
        let title = proposal_data
            .proposal
            .as_ref()
            .and_then(|proposal| proposal.title.clone())
            .unwrap_or("???".to_string());

        // If already marked as successful (from an earlier attempt??),
        // leave proposal_data alone.
        if proposal_data.executed_timestamp_seconds != 0 {
            println!(
                "{}Proposal {} (title: {}) already marked as executed. Ignoring new result.",
                LOG_PREFIX, proposal_id, title,
            );
            return;
        }

        // Handle fail.
        let encoded_reply: Vec<u8> = match result {
            Ok(ok) => ok,
            Err(error) => {
                println!(
                    "{}Failed to execute proposal {} (title: {}). Reason: {:?}",
                    LOG_PREFIX, proposal_id, title, error,
                );

                proposal_data.failed_timestamp_seconds = now_timestamp_seconds;
                proposal_data.failure_reason = Some(error);
                return;
            }
        };
```

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

**File:** rs/nns/governance/src/governance.rs (L3986-4006)
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

**File:** rs/nns/governance/src/governance.rs (L4025-4033)
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
```

**File:** rs/nns/governance/src/governance.rs (L4073-4076)
```rust
        let _ = self
            .reward_node_providers(&monthly_node_provider_rewards.rewards)
            .await;
        self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);
```
