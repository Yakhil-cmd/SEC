### Title
Unchecked Arithmetic Subtraction in `calculate_voting_rewards` Produces Erroneous Reward Calculation When `genesis_timestamp_seconds > now` - (File: `rs/nns/governance/src/governance.rs`)

### Summary

The `calculate_voting_rewards` function in the NNS Governance canister performs an unchecked integer subtraction `(now - self.heap_data.genesis_timestamp_seconds)` at line 6613. If `genesis_timestamp_seconds` exceeds `now` (i.e., genesis is set in the future), this subtraction underflows on `u64`, producing a massive erroneous `day_after_genesis` value. This causes the reward distribution loop to iterate over an astronomically large range of "days," leading to a panic or an incorrect reward event being recorded. The analogous function `most_recent_fully_elapsed_reward_round_end_timestamp_seconds` (line 3680) correctly guards against this with an explicit check, but `calculate_voting_rewards` does not.

### Finding Description

In `rs/nns/governance/src/governance.rs`, the private function `calculate_voting_rewards` computes the number of reward rounds elapsed since genesis:

```rust
let day_after_genesis =
    (now - self.heap_data.genesis_timestamp_seconds) / REWARD_DISTRIBUTION_PERIOD_SECONDS;
```

This is a plain `u64` subtraction with no underflow guard. If `self.heap_data.genesis_timestamp_seconds > now`, the subtraction wraps around to a value near `u64::MAX`, and dividing by `REWARD_DISTRIBUTION_PERIOD_SECONDS` (86400) still yields an enormous number. The resulting `days` range `last_event_day_after_genesis..day_after_genesis` would be astronomically large, causing the subsequent `.count()` call and iteration to either hang or produce a nonsensical `new_rounds_count`.

By contrast, the sibling function `most_recent_fully_elapsed_reward_round_end_timestamp_seconds` at line 3684 explicitly checks:

```rust
if genesis_timestamp_seconds > now {
    println!("{}WARNING: genesis is in the future...", ...);
    return 0;
}
```

No such guard exists in `calculate_voting_rewards`.

The `calculate_voting_rewards` function is called from `distribute_voting_rewards_to_neurons`, which is invoked by the `CalculateDistributableRewardsTask` timer task. This task runs automatically on a schedule derived from the governance state, without requiring any external trigger beyond the canister being live.

### Impact Explanation

If `genesis_timestamp_seconds` is set to a future value (e.g., via a governance upgrade or misconfiguration), `calculate_voting_rewards` will compute a near-`u64::MAX` value for `day_after_genesis`. The range `last_event_day_after_genesis..day_after_genesis` will have an astronomically large count. The `.count()` call on this range will itself iterate through ~`u64::MAX` values, effectively hanging the canister's periodic task execution indefinitely (a liveness failure). Even if the count somehow completes, the erroneous `day_after_genesis` value would be written into the `latest_reward_event`, permanently corrupting the reward accounting state of the NNS Governance canister. This is a **governance accounting bug** with potential **liveness impact** on the NNS.

### Likelihood Explanation

The `genesis_timestamp_seconds` field is set at canister initialization and is not normally modifiable post-deployment. However, the field is part of the serialized `GovernanceProto` state, meaning a canister upgrade that restores from a snapshot with a future genesis timestamp, or a bug in state migration, could trigger this. The `most_recent_fully_elapsed_reward_round_end_timestamp_seconds` function already documents this as a known edge case worth guarding against (it even logs a warning). The fact that one code path guards it and the other does not is a clear inconsistency. Likelihood is low in normal operation but non-zero during upgrades or state migrations.

### Recommendation

Add the same guard that exists in `most_recent_fully_elapsed_reward_round_end_timestamp_seconds` to `calculate_voting_rewards`. Specifically, before computing `day_after_genesis`, check whether `genesis_timestamp_seconds > now` and return `None` early (analogous to the `new_rounds_count == 0` early return):

```rust
if self.heap_data.genesis_timestamp_seconds > now {
    println!("{}WARNING: genesis is in the future, skipping reward distribution.", LOG_PREFIX);
    return None;
}
let day_after_genesis =
    (now - self.heap_data.genesis_timestamp_seconds) / REWARD_DISTRIBUTION_PERIOD_SECONDS;
```

Alternatively, use `saturating_sub`:
```rust
let day_after_genesis =
    now.saturating_sub(self.heap_data.genesis_timestamp_seconds) / REWARD_DISTRIBUTION_PERIOD_SECONDS;
```

### Proof of Concept

1. The unchecked subtraction is at: [1](#0-0) 

2. The analogous guarded version in the sibling function: [2](#0-1) 

3. The caller that triggers this periodically without external input: [3](#0-2) 

4. The `calculate_voting_rewards` function entry point: [4](#0-3)

### Citations

**File:** rs/nns/governance/src/governance.rs (L3684-3695)
```rust
        if genesis_timestamp_seconds > now {
            println!(
                "{}WARNING: genesis is in the future: {} vs. now = {})",
                LOG_PREFIX, genesis_timestamp_seconds, now,
            );
            return 0;
        }

        (now - genesis_timestamp_seconds) // Duration since genesis (in seconds).
            / REWARD_DISTRIBUTION_PERIOD_SECONDS // This is where the truncation happens. Whole number of rounds.
            * REWARD_DISTRIBUTION_PERIOD_SECONDS // Convert back into seconds.
            + self.heap_data.genesis_timestamp_seconds // Convert from duration to back to instant.
```

**File:** rs/nns/governance/src/governance.rs (L6602-6616)
```rust
    fn calculate_voting_rewards(
        &self,
        supply: Tokens,
    ) -> Option<(RewardEvent, Option<RewardsDistribution>)> {
        let now = self.env.now();

        let latest_reward_event = self.latest_reward_event();

        // Which reward rounds (i.e. days) require rewards? (Usually, there is
        // just one of these, but we support rewarding many consecutive rounds.)
        let day_after_genesis =
            (now - self.heap_data.genesis_timestamp_seconds) / REWARD_DISTRIBUTION_PERIOD_SECONDS;
        let last_event_day_after_genesis = latest_reward_event.day_after_genesis;
        let days = last_event_day_after_genesis..day_after_genesis;
        let new_rounds_count = days.clone().count();
```

**File:** rs/nns/governance/src/timer_tasks/calculate_distributable_rewards.rs (L51-75)
```rust
impl RecurringAsyncTask for CalculateDistributableRewardsTask {
    async fn execute(self) -> (Duration, Self) {
        let total_supply = self
            .governance
            .with_borrow(|governance| governance.get_ledger())
            .total_supply()
            .await;
        match total_supply {
            Ok(total_supply) => {
                self.governance.with_borrow_mut(|governance| {
                    governance.distribute_voting_rewards_to_neurons(total_supply);
                });
            }
            Err(err) => {
                println!(
                    "{}Error when getting total ICP supply: {}",
                    LOG_PREFIX,
                    GovernanceError::from(err)
                )
            }
        }

        let next_run = self.next_reward_task_from_now();
        (next_run, self)
    }
```
