### Title
Unbounded Loop Over Missed Reward Rounds in SNS `distribute_rewards` Can Permanently Break Reward Distribution - (File: `rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance `distribute_rewards` function contains an unbounded `for` loop that iterates once per missed reward round since the last distribution event. There is no cap on the number of iterations and no instruction-limit guard inside the loop. If `new_rounds_count` grows large enough — due to a short `round_duration_seconds` combined with any period of canister inactivity — the function will always trap due to IC instruction-limit exhaustion, permanently breaking SNS voting-reward distribution.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the total reward purse by looping over every missed round:

```rust
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);   // no upper bound
// ...
for i in 1..=new_rounds_count {               // unbounded
    let seconds_since_genesis = round_duration_seconds
        .saturating_mul(i)
        .saturating_add(reward_start_timestamp_seconds)
        .saturating_sub(self.proto.genesis_timestamp_seconds);

    let current_reward_rate = voting_rewards_parameters.reward_rate_at(
        crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
    );
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
```

`new_rounds_count` is derived purely from wall-clock time divided by `round_duration_seconds`. No maximum is enforced. There is no call to `is_message_over_threshold` or any equivalent instruction-limit check inside the loop body, unlike the NNS governance reward distribution path which uses `is_message_over_threshold` in `continue_processing`. [1](#0-0) [2](#0-1) 

By contrast, the NNS governance reward distribution path explicitly guards against instruction exhaustion:

```rust
let is_over_instructions_limit = || is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT);
// ...
if is_over_instructions_limit() {
    break;
}
``` [3](#0-2) [4](#0-3) 

The SNS `distribute_rewards` has no such guard. The NNS governance also has an analogous unbounded iteration (`days.map(...).sum()`) but its period is fixed at one day (`REWARD_DISTRIBUTION_PERIOD_SECONDS`), making it far harder to trigger in practice. [5](#0-4) 

### Impact Explanation

Once `new_rounds_count` is large enough to exhaust the per-message instruction limit (~5–40 billion instructions depending on message type), every call to `distribute_rewards` traps and the canister rolls back. Because `distribute_rewards` is called from `run_periodic_tasks` (the canister's heartbeat/timer), and the state is never updated on a trap, `latest_reward_event` is never advanced. On the next heartbeat, `new_rounds_count` is at least as large as before, so the function traps again — permanently. SNS voting rewards can never be distributed again. Neurons that voted accumulate no maturity. The SNS DAO's incentive mechanism is irreversibly broken without a canister upgrade. [6](#0-5) 

### Likelihood Explanation

`round_duration_seconds` is a governance-controlled parameter in `VotingRewardsParameters`. An SNS governance proposal can set it to a small value. Even at a moderate value (e.g., one hour = 3600 s), a canister upgrade that pauses execution for a few days, a subnet stall, or any other operational gap accumulates thousands of missed rounds. With a very short `round_duration_seconds` (e.g., seconds or minutes, if the validation floor permits it), even brief inactivity triggers the condition. The attack requires only a governance proposal — an action available to any sufficiently large SNS token holder — followed by waiting for any gap in heartbeat execution. [7](#0-6) 

### Recommendation

1. **Cap `new_rounds_count`** to a safe maximum per single execution (e.g., 1000 rounds), and advance `latest_reward_event.end_timestamp_seconds` by only `min(new_rounds_count, MAX_ROUNDS_PER_CALL) * round_duration_seconds` per invocation, allowing subsequent heartbeats to catch up incrementally.
2. **Add an instruction-limit guard** inside the loop body, mirroring the NNS pattern (`is_message_over_threshold`), so the function can break early and resume on the next timer tick.
3. **Enforce a minimum `round_duration_seconds`** in `VotingRewardsParameters` validation to bound the worst-case `new_rounds_count` for any realistic inactivity window.

### Proof of Concept

1. Deploy an SNS with `round_duration_seconds` set to a small value (e.g., 60 seconds).
2. Pause the SNS governance canister's heartbeat for a period long enough that `(now - reward_start_timestamp_seconds) / 60` exceeds the per-iteration instruction budget (e.g., stop the subnet for a few hours, or simulate via `set_time_warp` in a test).
3. Resume. Every subsequent call to `distribute_rewards` enters the `for i in 1..=new_rounds_count` loop with a count large enough to exhaust the instruction limit, trapping unconditionally.
4. Observe that `latest_reward_event` is never updated, `new_rounds_count` never decreases, and the trap repeats on every heartbeat — permanently locking SNS reward distribution. [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5763-5820)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
        let now = self.env.now();

        // VotingRewardsParameters should always be set,
        // but we check and return early just in case.
        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            Some(voting_rewards_parameters) => voting_rewards_parameters,
            None => {
                log!(
                    ERROR,
                    "distribute_rewards called even though \
                     voting_rewards_parameters not set.",
                );
                return;
            }
        };

        let round_duration_seconds = match voting_rewards_parameters.round_duration_seconds {
            Some(s) => s,
            None => {
                log!(
                    ERROR,
                    "round_duration_seconds not set:\n{:#?}",
                    voting_rewards_parameters,
                );
                return;
            }
        };
        // This guard is needed, because we'll divide by this amount shortly.
        if round_duration_seconds == 0 {
            // This is important, but emitting this every time will be spammy, because this gets
            // called during run_periodic_tasks.
            log!(
                ERROR,
                "round_duration_seconds ({}) is not positive. \
                 Therefore, we cannot calculate voting rewards.",
                round_duration_seconds,
            );
            return;
        }

        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
        if new_rounds_count == 0 {
            // This may happen, in case consider_distributing_rewards was called
            // several times at almost the same time. This is
            // harmless, just abandon.
            return;
        }
```

**File:** rs/sns/governance/src/governance.rs (L5861-5875)
```rust
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

**File:** rs/nns/governance/src/reward/distribution.rs (L43-51)
```rust
        let is_over_instructions_limit = || is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT);
        with_rewards_distribution_state_machine_mut(|rewards_distribution_state_machine| {
            rewards_distribution_state_machine.with_next_distribution(|(_, distribution)| {
                distribution
                    .continue_processing(&mut self.neuron_store, is_over_instructions_limit);
            });
            // Work left?
            !rewards_distribution_state_machine.distributions.is_empty()
        })
```

**File:** rs/nns/governance/src/reward/distribution.rs (L184-186)
```rust
            if is_over_instructions_limit() {
                break;
            }
```

**File:** rs/nns/governance/src/governance.rs (L6647-6649)
```rust
        let fraction: f64 = days
            .map(crate::reward::calculation::rewards_pool_to_distribute_in_supply_fraction_for_one_day)
            .sum();
```
