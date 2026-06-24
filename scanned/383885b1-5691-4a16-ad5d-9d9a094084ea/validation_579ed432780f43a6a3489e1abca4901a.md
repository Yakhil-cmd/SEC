### Title
Permanently Lost Node Provider Rewards When Minting Fails Due to Unconditional Period Advancement - (`rs/nns/governance/src/governance.rs`)

### Summary

In `mint_monthly_node_provider_rewards`, the result of `reward_node_providers` is silently discarded with `let _ =`, and `update_most_recent_monthly_node_provider_rewards` is called unconditionally regardless of whether any or all individual reward mints succeeded. This advances the reward period permanently, making it impossible to retry failed distributions. Node providers whose rewards failed to mint lose those rewards forever.

### Finding Description

The `mint_monthly_node_provider_rewards` function in `rs/nns/governance/src/governance.rs` computes the monthly reward amounts for all node providers, attempts to mint them, and then records the result as the "most recent" reward event:

```rust
let _ = self
    .reward_node_providers(&monthly_node_provider_rewards.rewards)
    .await;
self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);
``` [1](#0-0) 

Two compounding problems exist:

**1. The return value of `reward_node_providers` is discarded.** The `let _ =` pattern explicitly ignores the `Result<(), GovernanceError>` returned by `reward_node_providers`. Even if every single node provider's minting call fails, execution continues to the next line.

**2. `update_most_recent_monthly_node_provider_rewards` is called unconditionally.** This records the full intended `monthly_node_provider_rewards` (including all the amounts that were supposed to be minted) as the "most recent" reward event and updates the timestamp:

```rust
fn update_most_recent_monthly_node_provider_rewards(
    &mut self,
    most_recent_rewards: MonthlyNodeProviderRewards,
) {
    record_node_provider_rewards(most_recent_rewards.clone());
    self.heap_data.most_recent_monthly_node_provider_rewards = Some(most_recent_rewards);
}
``` [2](#0-1) 

The `most_recent_monthly_node_provider_rewards.timestamp` (or `end_date`) is then used by `next_start_date_node_providers_rewards` to determine the start of the next reward period: [3](#0-2) 

Once the period is advanced, the failed rewards are permanently unrecoverable — the system will never retry them.

Within `reward_node_providers` itself, individual failures are logged but processing continues for all remaining providers:

```rust
for reward in rewards {
    let reward_result = self.reward_node_provider_helper(reward).await;
    if reward_result.is_err() {
        println!("Rewarding {:?} failed. Reason: {:}", reward, reward_result.clone().unwrap_err());
    }
    result = result.or(reward_result);
}
``` [4](#0-3) 

The `monthly_node_provider_rewards` struct that gets recorded contains the full *intended* reward amounts, not the amounts actually minted. This is the direct IC analog of the Ajna H-08 pattern: the recorded/emitted value diverges from the actually transferred value, and the accounting state is advanced regardless.

### Impact Explanation

Node providers lose ICP rewards permanently when any transient or persistent error occurs during the monthly minting cycle. The `most_recent_monthly_node_provider_rewards` record falsely shows the full intended amounts as having been distributed, while the actual ICP was never minted. The next reward cycle starts from the advanced period, so there is no mechanism to recover or retry the lost rewards. This is a **ledger conservation bug**: ICP that should have been minted into node provider accounts is silently dropped.

### Likelihood Explanation

The monthly reward distribution involves multiple async inter-canister calls to the ICP ledger (one per node provider). Any transient ledger unavailability, invalid account identifier, or other error in `reward_node_provider_helper` causes that provider's reward to be silently dropped. The `is_time_to_mint_monthly_node_provider_rewards` guard ensures this runs once per month, so a single failed cycle means one full month of rewards is lost per affected provider. The periodic task is triggered automatically by the governance heartbeat — no attacker action is required; any ledger-side transient error suffices.

### Recommendation

1. **Do not discard the result of `reward_node_providers`.** If any reward fails, do not advance the reward period. Either revert the entire batch or track which providers were successfully paid and retry only the failed ones.

2. **Only call `update_most_recent_monthly_node_provider_rewards` after confirming all rewards were successfully minted**, or record the actual minted amounts rather than the intended amounts.

3. **Record per-provider success/failure** so that failed distributions can be retried in the next cycle without re-paying providers who were already successfully rewarded.

### Proof of Concept

The root cause is directly visible at: [1](#0-0) 

- Line 4073–4075: `let _ = self.reward_node_providers(...).await;` — result discarded.
- Line 4076: `self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);` — period advanced unconditionally.

A scenario: if the ICP ledger rejects a minting transfer for node provider A (e.g., due to a transient error), `reward_node_provider_helper` returns `Err`. `reward_node_providers` logs the error and returns `Err`. `mint_monthly_node_provider_rewards` discards the error and records the full reward set (including A's intended amount) as distributed. The next month's cycle starts from the new period. Node provider A's reward for this month is permanently lost with no on-chain record of the failure.

### Citations

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

**File:** rs/nns/governance/src/governance.rs (L4073-4076)
```rust
        let _ = self
            .reward_node_providers(&monthly_node_provider_rewards.rewards)
            .await;
        self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);
```

**File:** rs/nns/governance/src/governance.rs (L4091-4097)
```rust
    fn update_most_recent_monthly_node_provider_rewards(
        &mut self,
        most_recent_rewards: MonthlyNodeProviderRewards,
    ) {
        record_node_provider_rewards(most_recent_rewards.clone());
        self.heap_data.most_recent_monthly_node_provider_rewards = Some(most_recent_rewards);
    }
```

**File:** rs/nns/governance/src/governance.rs (L4134-4154)
```rust
    fn next_start_date_node_providers_rewards(&self) -> DateUtc {
        if let Some(rewards) = self.get_most_recent_monthly_node_provider_rewards() {
            if let Some(end_date) = rewards.end_date {
                // Case 1a: end_date exists → next start date is the day after
                let naive_date =
                    NaiveDate::from_ymd(end_date.year as i32, end_date.month, end_date.day);
                return DateUtc::from(naive_date.succ());
            }

            // Case 1b: end_date is None → fall back to the timestamp of the reward
            return DateUtc::from_unix_timestamp_seconds(rewards.timestamp);
        }

        // Case 2: No previous rewards → default start date
        // This is only used for test environments where there are no previous rewards.
        let default_ts = Time::from_nanos_since_unix_epoch(ic_cdk::api::time())
            .as_secs_since_unix_epoch()
            .saturating_sub(NODE_PROVIDER_REWARD_PERIOD_SECONDS);

        DateUtc::from_unix_timestamp_seconds(default_ts)
    }
```
