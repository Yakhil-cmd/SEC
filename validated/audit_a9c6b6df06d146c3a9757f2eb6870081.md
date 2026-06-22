### Title
Node Provider Reward Entirely Rejected Instead of Capped When Exceeding `maximum_node_provider_rewards_e8s`, Causing Permanent ICP Loss - (File: rs/nns/governance/src/governance.rs)

---

### Summary

In `rs/nns/governance/src/governance.rs`, the function `reward_node_provider_helper` rejects a node provider's entire monthly reward with an error when the calculated ICP amount exceeds `maximum_node_provider_rewards_e8s`, instead of capping the payment at the maximum. Because `mint_monthly_node_provider_rewards` discards the error result and unconditionally advances the monthly reward timestamp, the node provider receives **zero** ICP for that period and the reward is permanently lost — an exact structural analog to H-01.

---

### Finding Description

The monthly node provider reward flow is:

1. `mint_monthly_node_provider_rewards` calls `get_node_providers_rewards` (or `get_monthly_node_provider_rewards`) to compute each node provider's ICP reward via `get_node_provider_reward`, which performs a raw XDR→ICP conversion with **no cap applied**: [1](#0-0) 

2. The resulting `RewardNodeProvider` structs (with uncapped `amount_e8s`) are passed to `reward_node_providers`, which calls `reward_node_provider_helper` for each one.

3. Inside `reward_node_provider_helper`, if `reward.amount_e8s > maximum_node_provider_rewards_e8s`, the function returns a hard `Err` — **no ICP is minted, not even the capped maximum**: [2](#0-1) 

4. Back in `reward_node_providers`, the per-provider error is only logged; processing continues for other providers: [3](#0-2) 

5. Critically, `mint_monthly_node_provider_rewards` discards the aggregate error result with `let _` and then unconditionally calls `update_most_recent_monthly_node_provider_rewards`, advancing the monthly reward timestamp: [4](#0-3) 

Because the timestamp is advanced, the next reward period starts fresh. The node provider whose reward was rejected receives **zero** ICP for that month, and the entitled reward is permanently unrecoverable.

---

### Impact Explanation

A node provider whose calculated monthly ICP reward exceeds `maximum_node_provider_rewards_e8s` loses their **entire** monthly reward rather than receiving the capped maximum. The correct behavior would be to mint `min(calculated_amount, maximum_node_provider_rewards_e8s)`. Instead, the provider receives zero. This is a ledger conservation bug: ICP that should have been minted to a legitimate node provider is silently discarded.

The `maximum_node_provider_rewards_e8s` field is snapshotted in `MonthlyNodeProviderRewards` for auditing purposes but is never used to clamp the reward at calculation time: [5](#0-4) 

---

### Likelihood Explanation

The trigger condition — a node provider's XDR reward converting to more ICP e8s than `maximum_node_provider_rewards_e8s` — can occur without any privileged action:

- The ICP/XDR conversion rate is fetched from the CMC as a 30-day average. A sustained drop in ICP price causes the same XDR reward to convert to a larger e8s amount.
- Large node providers with many type-0/type-1 nodes in high-reward regions can accumulate XDR rewards that, at low ICP prices, exceed the cap.
- No governance vote or admin key is required; the periodic task `mint_monthly_node_provider_rewards` fires automatically. [6](#0-5) 

---

### Recommendation

In `reward_node_provider_helper`, replace the hard rejection with a cap:

```rust
let amount_to_mint = reward.amount_e8s.min(maximum_node_provider_rewards_e8s);
// use amount_to_mint instead of reward.amount_e8s when calling mint_reward_to_neuron_or_account
```

Additionally, the result of `reward_node_providers` in `mint_monthly_node_provider_rewards` should not be silently discarded; at minimum, a failed reward should prevent the monthly timestamp from advancing for the affected provider so the reward can be retried.

---

### Proof of Concept

1. Node provider NP-A has 500 type-0 nodes in a high-reward region. Their monthly XDR reward is 5,000,000 XDR permyriad.
2. The 30-day average ICP/XDR rate drops to 1 XDR per ICP (i.e., `xdr_permyriad_per_icp = 10000`).
3. `get_node_provider_reward` computes: `(5_000_000 * 100_000_000) / 10_000 = 50_000_000_000_000 e8s = 500,000 ICP`.
4. `maximum_node_provider_rewards_e8s` is set to `100_000_000_000_000` (1,000,000 ICP default) — but suppose it was previously lowered to `10_000_000_000_000` (100,000 ICP) via a `ManageNetworkEconomics` proposal, or the rate drops further.
5. `reward_node_provider_helper` sees `500_000 ICP > 100_000 ICP` and returns `Err(PreconditionFailed)`.
6. `reward_node_providers` logs the error and continues.
7. `mint_monthly_node_provider_rewards` discards the error and calls `update_most_recent_monthly_node_provider_rewards`, advancing the timestamp.
8. NP-A receives 0 ICP. The next month's calculation starts fresh; the 500,000 ICP reward is permanently lost. [7](#0-6) [8](#0-7)

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

**File:** rs/nns/governance/src/governance.rs (L3993-4003)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L4067-4076)
```rust
        let monthly_node_provider_rewards = if are_performance_based_rewards_enabled() {
            self.get_node_providers_rewards().await?
        } else {
            self.get_monthly_node_provider_rewards().await?
        };

        let _ = self
            .reward_node_providers(&monthly_node_provider_rewards.rewards)
            .await;
        self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);
```

**File:** rs/nns/governance/src/governance.rs (L7678-7708)
```rust
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

        let xdr_conversion_rate = XdrConversionRate {
            timestamp_seconds: icp_xdr_conversion_rate.timestamp_seconds,
            xdr_permyriad_per_icp: icp_xdr_conversion_rate.xdr_permyriad_per_icp,
        };

        Ok(MonthlyNodeProviderRewards {
            timestamp: now,
            start_date: Some(pb::v1::DateUtc::try_from(start_date).expect("from date exists")),
            end_date: Some(pb::v1::DateUtc::try_from(end_date).expect("to date exists")),
            rewards,
            xdr_conversion_rate: Some(xdr_conversion_rate.into()),
            minimum_xdr_permyriad_per_icp: Some(minimum_xdr_permyriad_per_icp),
            maximum_node_provider_rewards_e8s: Some(maximum_node_provider_rewards_e8s),
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
