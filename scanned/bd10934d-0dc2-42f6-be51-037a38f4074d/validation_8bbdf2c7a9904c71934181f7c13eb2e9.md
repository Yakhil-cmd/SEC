### Title
Monthly Node Provider ICP Rewards Permanently Lost on Ledger Failure — (`File: rs/nns/governance/src/governance.rs`)

### Summary

In `mint_monthly_node_provider_rewards`, the result of `reward_node_providers` is unconditionally discarded with `let _ = ...`, and `update_most_recent_monthly_node_provider_rewards` is called regardless of whether any minting transfers succeeded or failed. If the ICP ledger is temporarily unavailable during the monthly distribution window, all node provider rewards for that month are silently and permanently lost with no retry or rollover mechanism.

### Finding Description

`mint_monthly_node_provider_rewards` in `rs/nns/governance/src/governance.rs` orchestrates the monthly ICP reward minting for all node providers. After computing the reward amounts, it calls `reward_node_providers`, which iterates over every `RewardNodeProvider` entry and calls `mint_reward_to_neuron_or_account` (a ledger `transfer_funds` call) for each one. [1](#0-0) 

The critical flaw is on line 4073: the return value of `reward_node_providers` is bound to `_` and thrown away. Immediately after, `update_most_recent_monthly_node_provider_rewards` is called unconditionally, recording the current timestamp as the last successful distribution. [2](#0-1) 

`is_time_to_mint_monthly_node_provider_rewards` uses this timestamp to gate the next distribution: [3](#0-2) 

Because the timestamp is always written — even when every minting transfer failed — the governance canister will not attempt to re-distribute the missed rewards. There is no rollover of failed amounts and no retry queue.

The inner loop in `reward_node_providers` does continue past individual failures: [4](#0-3) 

But the accumulated `Err` result is never inspected by the caller, so the information is discarded.

### Impact Explanation

Every node provider loses their entire monthly ICP reward for any month in which the ICP ledger is temporarily unavailable at the moment the governance timer fires the distribution. The ICP is never minted; it does not exist anywhere in the system. There is no on-chain record of the failure and no mechanism to recover or re-issue the missed rewards. This is a permanent, irreversible ledger conservation loss.

### Likelihood Explanation

The ICP ledger undergoes routine upgrades (typically several times per month). During an upgrade the ledger is briefly unavailable and rejects calls. The monthly reward timer fires once every `NODE_PROVIDER_REWARD_PERIOD_SECONDS` (~30 days). If the timer fires during a ledger upgrade window — a realistic coincidence — all minting calls return errors, the errors are silently dropped, and the monthly timestamp is committed. The probability per month is low but non-negligible given the frequency of ledger upgrades and the fact that the timer fires at a deterministic offset from genesis.

### Recommendation

1. Do not discard the result of `reward_node_providers`. If any minting transfer fails, do **not** call `update_most_recent_monthly_node_provider_rewards`, so the distribution will be retried on the next timer tick.
2. Alternatively, implement a per-node-provider retry queue: record which rewards were successfully minted and which were not, and carry failed entries forward to the next distribution cycle.
3. Emit an explicit on-chain event or metric when a reward minting fails so operators can detect and manually remediate the situation.

### Proof of Concept

1. The NNS governance timer fires `mint_monthly_node_provider_rewards`.
2. `get_node_providers_rewards` (or `get_monthly_node_provider_rewards`) succeeds and returns a list of `RewardNodeProvider` entries for all active node providers.
3. The ICP ledger is in the middle of an upgrade; every call to `transfer_funds` inside `reward_node_provider_helper` → `mint_reward_to_neuron_or_account` returns `Err(...)`.
4. `reward_node_providers` accumulates the errors and returns `Err(...)`.
5. Line 4073: `let _ = self.reward_node_providers(...).await;` — the `Err` is silently dropped.
6. Line 4076: `self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);` — the timestamp is written as if distribution succeeded.
7. The ledger upgrade completes. The next timer tick checks `is_time_to_mint_monthly_node_provider_rewards`, finds that fewer than `NODE_PROVIDER_REWARD_PERIOD_SECONDS` have elapsed since the (falsely recorded) last distribution, and skips the distribution.
8. All node providers have permanently lost one month of ICP rewards with no on-chain indication of the failure. [5](#0-4)

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

**File:** rs/nns/governance/src/governance.rs (L4023-4033)
```rust
    /// Return `true` if `NODE_PROVIDER_REWARD_PERIOD_SECONDS` has passed since the last monthly
    /// node provider reward event
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

**File:** rs/nns/governance/src/governance.rs (L4040-4089)
```rust
    async fn mint_monthly_node_provider_rewards(&mut self) -> Result<(), GovernanceError> {
        // Return immediately if another call is already in progress.
        thread_local! {
            static LOCK: RefCell<Option<u64>> = const { RefCell::new(None) };
        }
        let release_on_drop = ic_nervous_system_lock::acquire(&LOCK, self.env.now());
        if let Err(earlier_call_start_timestamp) = release_on_drop {
            // Log, but not too frequently (at most once every 5 minutes).
            thread_local! {
                static LAST_LOGGED_UNAVAILABLE_TIMESTAMP_SECONDS: RefCell<u64> = const { RefCell::new(0) };
            }
            let time_since_logged_seconds = LAST_LOGGED_UNAVAILABLE_TIMESTAMP_SECONDS
                .with(|t| self.env.now().saturating_sub(*t.borrow()));
            if time_since_logged_seconds > 5 * 60 {
                println!(
                    "{}Another mint_monthly_node_provider_rewards call (started at \
                     {} seconds since the UNIX epoch) is already in progress.",
                    LOG_PREFIX, earlier_call_start_timestamp,
                );
                LAST_LOGGED_UNAVAILABLE_TIMESTAMP_SECONDS.with(|t| {
                    *t.borrow_mut() = self.env.now();
                });
            }

            return Ok(());
        }

        let monthly_node_provider_rewards = if are_performance_based_rewards_enabled() {
            self.get_node_providers_rewards().await?
        } else {
            self.get_monthly_node_provider_rewards().await?
        };

        let _ = self
            .reward_node_providers(&monthly_node_provider_rewards.rewards)
            .await;
        self.update_most_recent_monthly_node_provider_rewards(monthly_node_provider_rewards);

        // Commit the minting status by making a canister call.
        let _unused_canister_status_response = self
            .env
            .call_canister_method(
                GOVERNANCE_CANISTER_ID,
                "get_build_metadata",
                Encode!().unwrap_or_default(),
            )
            .await;

        Ok(())
    }
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
