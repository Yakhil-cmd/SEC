### Title
SNS Governance `run_periodic_tasks()` Applies Updated Reward Parameters Before Settling Past-Period Rewards - (`rs/sns/governance/src/governance.rs`)

### Summary

In the SNS governance canister, `run_periodic_tasks()` calls `process_proposals()` synchronously before calling `distribute_rewards()`. If a `ManageNervousSystemParameters` proposal that changes `voting_rewards_parameters` (e.g., `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, or `reward_rate_transition_duration_seconds`) is executed during `process_proposals()`, the subsequent `distribute_rewards()` call computes the reward purse for all unsettled past rounds using the **new** parameters rather than the parameters that were in effect during those rounds. This is a direct analog to M-06: rate is updated first, then old rewards are settled using the new rate.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `run_periodic_tasks()` executes the following sequence:

```
1. self.process_proposals()          // may execute ManageNervousSystemParameters
2. should_distribute_rewards = ...   // reads new round_duration_seconds
3. self.ledger.total_supply().await  // async boundary
4. self.distribute_rewards(supply)   // reads new voting_rewards_parameters
``` [1](#0-0) 

`process_proposals()` calls `perform_manage_nervous_system_parameters()`, which immediately overwrites `self.proto.parameters` with the new `NervousSystemParameters`: [2](#0-1) 

Then `distribute_rewards()` reads `voting_rewards_parameters` from the now-updated `self.proto.parameters` and uses them to compute the reward purse for **all** unsettled rounds since the last `RewardEvent`: [3](#0-2) 

The reward purse loop iterates over every missed round and calls `reward_rate_at()` using the **current** (post-update) parameters: [4](#0-3) 

`reward_rate_at()` computes the rate purely from the current `initial_reward_rate_basis_points`, `final_reward_rate_basis_points`, and `reward_rate_transition_duration_seconds` — there is no historical snapshot: [5](#0-4) 

If the new parameters specify a lower reward rate, all unsettled rounds are retroactively under-rewarded. If the new parameters specify a higher rate, they are over-rewarded. The magnitude of the error scales with the number of unsettled rounds (which can be many if reward distribution was delayed due to no proposals being ready to settle).

### Impact Explanation

Neuron holders lose (or gain) maturity for every unsettled reward round at the moment a `ManageNervousSystemParameters` proposal changing reward rate parameters is executed. The loss is proportional to `(old_rate - new_rate) × round_duration × token_supply × number_of_unsettled_rounds`. For an SNS with a large token supply and multiple missed rounds, this can be a material loss of yield for all voting participants. The `total_available_e8s_equivalent` field in the `RewardEvent` will also be incorrect, permanently misrepresenting the historical reward pool. [6](#0-5) 

### Likelihood Explanation

Any legitimately-passed `ManageNervousSystemParameters` proposal that modifies `voting_rewards_parameters` triggers this bug. No malicious intent is required — a well-intentioned inflation-reduction proposal is sufficient. The SNS governance canister's `run_periodic_tasks()` is called on every heartbeat, and `process_proposals()` executes ready proposals synchronously before the reward distribution check. The window is not narrow: the bug fires whenever a parameter-change proposal happens to be ready to execute in the same heartbeat as a reward distribution event, which is a natural coincidence given that both are periodic. [7](#0-6) 

### Recommendation

Snapshot `voting_rewards_parameters` at the **start** of each reward round (e.g., store them in the `RewardEvent` proto) and use the snapshotted values when computing the purse for that round. Alternatively, call `distribute_rewards()` **before** `process_proposals()` in `run_periodic_tasks()`, so that any parameter changes only take effect for future rounds. This mirrors the fix recommended in M-06: settle old rewards before applying the new rate.

### Proof of Concept

Given:
- SNS with `initial_reward_rate_basis_points = 200` (2%), `final_reward_rate_basis_points = 100` (1%), `round_duration_seconds = 86400` (1 day)
- Last `RewardEvent` was 3 days ago (3 unsettled rounds due to no proposals)
- A `ManageNervousSystemParameters` proposal reducing `initial_reward_rate_basis_points` to `50` (0.5%) is ready to execute
- Token supply = 1,000,000,000 e8s

**Expected**: Rounds 1–3 are settled at the old rate (~2%), yielding approximately `3 × (0.02/365) × 1e9 ≈ 164,384 e8s` in maturity.

**Actual**: `process_proposals()` executes the parameter change first. `distribute_rewards()` then computes all 3 rounds at the new rate (~0.5%), yielding approximately `3 × (0.005/365) × 1e9 ≈ 41,096 e8s` — a loss of ~75% of expected rewards for those 3 rounds. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L2595-2598)
```rust
        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L5471-5521)
```rust
    /// Runs periodic tasks that are not directly triggered by user input.
    pub async fn run_periodic_tasks(&mut self) {
        use ic_cdk::println;

        self.process_proposals();

        // None of the upgrade-related tasks should interleave with one another or themselves, so we acquire a global
        // lock for the duration of their execution. This will return `false` if the lock has already been acquired less
        // than 10 minutes ago by a previous invocation of `run_periodic_tasks`, in which case we skip the
        // upgrade-related tasks.
        if self.acquire_upgrade_periodic_task_lock() {
            // We only want to check the upgrade status if we are currently executing an upgrade.
            if self.should_check_upgrade_status() {
                self.check_upgrade_status().await;
            }

            if self.should_refresh_cached_upgrade_steps() {
                match self.try_temporarily_lock_refresh_cached_upgrade_steps() {
                    Err(err) => {
                        log!(ERROR, "{}", err);
                    }
                    Ok(deployed_version) => {
                        self.refresh_cached_upgrade_steps(deployed_version).await;
                    }
                }
            }

            self.initiate_upgrade_if_sns_behind_target_version().await;

            self.release_upgrade_periodic_task_lock();
        }

        let should_distribute_rewards = self.should_distribute_rewards();

        // Getting the total governance token supply from the ledger is expensive enough
        // that we don't want to do it on every call to `run_periodic_tasks`. So
        // we only fetch it when it's needed, which is when rewards should be
        // distributed
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }
```

**File:** rs/sns/governance/src/governance.rs (L5769-5773)
```rust
        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            Some(voting_rewards_parameters) => voting_rewards_parameters,
```

**File:** rs/sns/governance/src/governance.rs (L5854-5875)
```rust
        let rewards_purse_e8s = {
            let mut result = Decimal::from(
                self.latest_reward_event()
                    .e8s_equivalent_to_be_rolled_over(),
            );
            let supply = i2d(supply.get_e8s());

            for i in 1..=new_rounds_count {
                let seconds_since_genesis = round_duration_seconds
                    .saturating_mul(i)
                    .saturating_add(reward_start_timestamp_seconds)
                    .saturating_sub(self.proto.genesis_timestamp_seconds);

                let current_reward_rate = voting_rewards_parameters.reward_rate_at(
                    crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
                );

                result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
            }

            result
        };
```

**File:** rs/sns/governance/src/governance.rs (L5999-6011)
```rust
        // Freeze distributed_e8s_equivalent, now that we are done handing out rewards.
        let distributed_e8s_equivalent = distributed_e8s_equivalent;
        // Because we used floor to round rewards to integers (and everything is
        // non-negative), it should be that the amount distributed is not more
        // than the original purse.
        debug_assert!(
            i2d(distributed_e8s_equivalent) <= rewards_purse_e8s,
            "rewards distributed ({distributed_e8s_equivalent}) > purse ({rewards_purse_e8s})",
        );

        // This field is deprecated. People should really use end_timestamp_seconds
        // instead. This value can still be used if round duration is not changed.
        let new_reward_event_round = self.latest_reward_event().round + new_rounds_count;
```

**File:** rs/sns/governance/src/reward.rs (L197-242)
```rust
    pub fn reward_rate_at(&self, now: Instant) -> RewardRate {
        let reward_rate_transition_duration_seconds = self
            .reward_rate_transition_duration_seconds
            .expect("reward_rate_transition_duration_seconds unset");

        let time_since_genesis = {
            let result = now - *GENESIS;
            // For the purposes of determining reward rate, treat times before
            // genesis the same as at genesis. This is not expected to occur in
            // practice. This code is just being extra defensive.
            if result.as_secs() < i2d(0) {
                Duration { days: i2d(0) }
            } else {
                result
            }
        };
        if reward_rate_transition_duration_seconds == 0
            || time_since_genesis.as_secs() >= i2d(reward_rate_transition_duration_seconds)
        {
            return self.final_reward_rate();
        }

        // s linearly varies from 1 -> 0 as seconds_since_genesis varies from 0
        // to reward_rate_transition_duration_seconds.
        let transition = LinearMap::new(
            dec!(0)..i2d(reward_rate_transition_duration_seconds),
            dec!(1)..dec!(0),
        );
        let s = transition.apply(time_since_genesis.as_secs());
        // s2 varies quadratically from 1 -> 0 (again, as seconds_since_genesis
        // varies from 0 to reward_rate_transition_duration_seconds), and
        // flattens out as seconds_since_genesis approaches
        // reward_rate_transition_duration_seconds.
        let s2 = s * s;

        // This looks backwards, but we think of variable rate as being added to
        // final growth rate, not initial, and the amount to add is up to
        // initial - final (where initial is thought of as being greater than
        // final).
        let dr = self.initial_reward_rate() - self.final_reward_rate();
        // variable_reward_rate varies from dr to 0 as round varies from
        // 1 to transition_round_count.
        let variable_reward_rate = s2 * dr;

        self.final_reward_rate() + variable_reward_rate
    }
```
