### Title
SNS Governance Voting Reward Distribution Permanently Blocked by u64 Overflow in Rewards Purse — (File: `rs/sns/governance/src/governance.rs`)

### Summary

The `distribute_rewards` function in the SNS governance canister returns early — without updating `self.proto.latest_reward_event` — when the accumulated `rewards_purse_e8s` value overflows `u64`. Because the reward event timestamp is never advanced, every subsequent periodic invocation recomputes an even larger purse over an ever-growing `new_rounds_count`, making the overflow self-reinforcing and the block permanent until a canister upgrade.

### Finding Description

`distribute_rewards` accumulates the rewards purse as a `Decimal` over all missed reward rounds:

```rust
// rs/sns/governance/src/governance.rs  lines 5854-5875
let rewards_purse_e8s = {
    let mut result = Decimal::from(
        self.latest_reward_event().e8s_equivalent_to_be_rolled_over(),
    );
    let supply = i2d(supply.get_e8s());
    for i in 1..=new_rounds_count {
        ...
        result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
    }
    result
};
```

It then attempts to narrow the `Decimal` to `u64`:

```rust
// lines 5878-5889
let total_available_e8s_equivalent = Some(match u64::try_from(rewards_purse_e8s) {
    Ok(ok) => ok,
    Err(err) => {
        log!(ERROR, "Looks like the rewards purse ({}) overflowed u64: {}. \
             Therefore, we stop the current attempt to distribute voting rewards.",
             rewards_purse_e8s, err);
        return;   // ← early return, no state mutation
    }
});
```

The only place `self.proto.latest_reward_event` is updated is at the very end of the function:

```rust
// lines 6084-6092
self.proto.latest_reward_event = Some(RewardEvent {
    round: new_reward_event_round,
    ...
    end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
    ...
});
```

Because the early `return` fires before this assignment, `reward_start_timestamp_seconds` (derived from `latest_reward_event.end_timestamp_seconds`) never advances. On the next timer tick, `new_rounds_count` is larger still, `rewards_purse_e8s` is larger still, and the overflow repeats indefinitely.

The `e8s_equivalent_to_be_rolled_over` helper confirms that the stale rolled-over amount is re-added on every attempt:

```rust
// rs/sns/governance/src/types.rs  lines 2054-2059
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent.unwrap_or_default()
    } else { 0 }
}
```

### Impact Explanation

Once the overflow condition is reached, every subsequent call to `distribute_rewards` (invoked automatically by the SNS periodic-task timer) returns early. SNS neuron holders stop receiving voting-reward maturity. Proposals that have reached `ReadyToSettle` status remain stuck with their ballots un-cleared, consuming heap memory indefinitely. The only recovery path is a canister upgrade — exactly the situation described in the reference report.

### Likelihood Explanation

`rewards_purse_e8s` grows as `supply_e8s × reward_rate_per_round × new_rounds_count`. For a maximum-supply SNS token (≈ 1.84 × 10¹⁹ e8s) with a 10 % annual reward rate and daily rounds, overflow occurs after roughly 3 650 missed rounds (≈ 10 years). With a shorter `round_duration_seconds` or a higher `initial_reward_rate_percentage`, the threshold is proportionally lower. A canister pause during a complex upgrade, a subnet stall, or a governance deadlock that prevents periodic tasks from running for an extended period can push `new_rounds_count` into the danger zone. The self-reinforcing nature of the bug means a single overflow event is sufficient to make the block permanent.

### Recommendation

Replace the early `return` with a saturating cap or a capped-distribution strategy so that `latest_reward_event` is always advanced, even when the purse is abnormally large. For example, cap `rewards_purse_e8s` at `u64::MAX` and distribute that capped amount, or split the backfill into bounded batches (similar to how the NNS governance already handles large distributions via `RewardsDistributionStateMachine`). At minimum, the function should advance `latest_reward_event.end_timestamp_seconds` even when it cannot distribute, so that `new_rounds_count` does not grow without bound.

### Proof of Concept

1. Deploy an SNS with a large token supply (e.g., `total_tokens_distributed` near `u64::MAX / 10^8`) and a high `initial_reward_rate_percentage` (e.g., 100 %).
2. Pause the SNS governance canister (e.g., via a stop-canister proposal) for a period long enough that `new_rounds_count × supply_e8s × rate_per_round > u64::MAX`.
3. Resume the canister. The next invocation of `run_periodic_tasks` calls `distribute_rewards`.
4. `u64::try_from(rewards_purse_e8s)` returns `Err`; the function logs the error and returns without updating `self.proto.latest_reward_event`.
5. Every subsequent periodic-task invocation repeats step 4 with an ever-larger `new_rounds_count`.
6. SNS neuron maturity stops increasing; `ReadyToSettle` proposals accumulate indefinitely.

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L5878-5889)
```rust
        let total_available_e8s_equivalent = Some(match u64::try_from(rewards_purse_e8s) {
            Ok(ok) => ok,
            Err(err) => {
                log!(
                    ERROR,
                    "Looks like the rewards purse ({}) overflowed u64: {}. \
                     Therefore, we stop the current attempt to distribute voting rewards.",
                    rewards_purse_e8s,
                    err,
                );
                return;
            }
```

**File:** rs/sns/governance/src/governance.rs (L6083-6093)
```rust
        // Conclude this round of rewards.
        self.proto.latest_reward_event = Some(RewardEvent {
            round: new_reward_event_round,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent,
            end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
            rounds_since_last_distribution: Some(new_rounds_count),
            total_available_e8s_equivalent,
        })
    }
```

**File:** rs/sns/governance/src/types.rs (L2054-2059)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
```
