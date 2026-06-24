### Title
Unbounded Loop in `distribute_rewards` Due to Insufficient Minimum `round_duration_seconds` Enforcement Causes Instruction-Limit DoS - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister's `distribute_rewards` function contains an unbounded loop whose iteration count is `new_rounds_count = (now - reward_start_timestamp_seconds) / round_duration_seconds`. The `round_duration_seconds` parameter is validated only to be in the range `1..=MAX_REWARD_ROUND_DURATION_SECONDS` (1 second to 365.25 days). When set to its minimum of 1 second, and when `reward_start_timestamp_seconds` defaults to 0 (because the genesis `RewardEvent.end_timestamp_seconds` is `None`), `new_rounds_count` equals the current Unix timestamp (~1.75 billion). The resulting loop exhausts the IC instruction limit, causing the canister to trap on every invocation of `run_periodic_tasks`, permanently preventing voting-reward distribution.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();          // → 0 for a fresh SNS
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
``` [1](#0-0) 

It then iterates unconditionally over every missed round:

```rust
for i in 1..=new_rounds_count {
    ...
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
``` [2](#0-1) 

There is no cap on `new_rounds_count` and no instruction-limit check inside the loop. The only guard is a zero-check:

```rust
if new_rounds_count == 0 { return; }
``` [3](#0-2) 

The `round_duration_seconds` validation enforces only `1..=MAX_REWARD_ROUND_DURATION_SECONDS`:

```rust
fn round_duration_seconds_defects(&self) -> Vec<String> {
    require_field_set_and_in_range(
        "round_duration_seconds",
        &self.round_duration_seconds,
        1..=*MAX_REWARD_ROUND_DURATION_SECONDS,
    )
}
``` [4](#0-3) 

where `MAX_REWARD_ROUND_DURATION_SECONDS = 31_557_600` (365.25 days). The minimum of **1 second** is the root cause: it is too small to prevent `new_rounds_count` from reaching billions.

The NNS governance equivalent (`calculate_voting_rewards`) is not affected because its period is the hard-coded constant `REWARD_DISTRIBUTION_PERIOD_SECONDS` (86 400 s), bounding `new_rounds_count` to the number of days since genesis (~1 800). [5](#0-4) 

### Impact Explanation

When `distribute_rewards` is called from `run_periodic_tasks` (the SNS heartbeat/timer path), the canister traps with `InstructionLimitExceeded`. Because `reward_start_timestamp_seconds` is updated only on successful completion of `distribute_rewards`, the trap is permanent: every subsequent timer invocation recomputes the same enormous `new_rounds_count` and traps again. The SNS governance canister enters a Denial-of-Service state in which:

- Voting rewards are never distributed to any neuron.
- The `run_periodic_tasks` timer path is permanently broken, blocking other periodic work that shares the same execution path.

### Likelihood Explanation

An SNS is deployed by submitting a `CreateServiceNervousSystem` NNS proposal. The `round_duration_seconds` field is set at deployment time and is validated only to be ≥ 1. A deployer who sets `round_duration_seconds = 1` (or any value small enough that `(now - genesis) / round_duration_seconds` exceeds the per-message instruction budget of ~5 billion instructions) will trigger the DoS on the first timer firing. Alternatively, any SNS governance majority can pass a `ManageNervousSystemParameters` proposal to reduce `round_duration_seconds` to 1 at any time after deployment, triggering the same effect. The attack requires no special cryptographic key or subnet-majority corruption.

### Recommendation

1. **Enforce a meaningful minimum for `round_duration_seconds`** — e.g., 3 600 s (1 hour) or higher — so that `new_rounds_count` is bounded to a value that cannot exhaust the instruction limit within a single message execution.
2. **Cap `new_rounds_count` inside `distribute_rewards`** — add an explicit upper bound (e.g., `new_rounds_count.min(MAX_ROUNDS_PER_DISTRIBUTION)`) and log a warning when the cap is hit, analogous to how the NNS governance already warns when `new_rounds_count > 1`.
3. **Add an instruction-limit check inside the loop** — mirror the pattern used in `continue_processing` for reward distribution, which checks `is_over_instructions_limit()` after each iteration and breaks early. [6](#0-5) 

### Proof of Concept

1. Deploy an SNS with `voting_rewards_parameters.round_duration_seconds = 1` (passes the current `validate()` check since `1 ≥ 1`).
2. The genesis `RewardEvent` has `end_timestamp_seconds = None`, so `reward_start_timestamp_seconds = 0`.
3. On the first timer firing, `should_distribute_rewards` returns `true` because `now > 1`.
4. Inside `distribute_rewards`, `new_rounds_count = now / 1 ≈ 1_750_000_000`.
5. The `for i in 1..=new_rounds_count` loop attempts ~1.75 billion iterations of floating-point arithmetic, exhausting the ~5 billion instruction budget and causing `InstructionLimitExceeded`.
6. The canister traps; `latest_reward_event` is not updated; the next timer invocation repeats identically — permanent DoS.

### Citations

**File:** rs/sns/governance/src/governance.rs (L5808-5814)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
```

**File:** rs/sns/governance/src/governance.rs (L5815-5820)
```rust
        if new_rounds_count == 0 {
            // This may happen, in case consider_distributing_rewards was called
            // several times at almost the same time. This is
            // harmless, just abandon.
            return;
        }
```

**File:** rs/sns/governance/src/governance.rs (L5861-5872)
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
```

**File:** rs/sns/governance/src/reward.rs (L254-260)
```rust
    fn round_duration_seconds_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "round_duration_seconds",
            &self.round_duration_seconds,
            1..=*MAX_REWARD_ROUND_DURATION_SECONDS,
        )
    }
```

**File:** rs/nns/governance/src/governance.rs (L6612-6616)
```rust
        let day_after_genesis =
            (now - self.heap_data.genesis_timestamp_seconds) / REWARD_DISTRIBUTION_PERIOD_SECONDS;
        let last_event_day_after_genesis = latest_reward_event.day_after_genesis;
        let days = last_event_day_after_genesis..day_after_genesis;
        let new_rounds_count = days.clone().count();
```

**File:** rs/nns/governance/src/reward/distribution.rs (L154-188)
```rust
    fn continue_processing(
        &mut self,
        neuron_store: &mut NeuronStore,
        is_over_instructions_limit: fn() -> bool,
    ) {
        while let Some((id, reward_e8s)) = self.rewards.pop_first() {
            match neuron_store.with_neuron_mut(&id, |neuron| {
                let auto_stake = neuron.auto_stake_maturity.unwrap_or(false);
                if auto_stake {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron
                            .staked_maturity_e8s_equivalent
                            .unwrap_or_default()
                            .saturating_add(reward_e8s),
                    );
                } else {
                    neuron.maturity_e8s_equivalent =
                        neuron.maturity_e8s_equivalent.saturating_add(reward_e8s);
                }
            }) {
                Ok(_) => {}
                Err(e) => {
                    println!(
                        "{}Error rewarding neuron {:?} during reward_distribution.\
                    This should not be possible as neuron existence is checked when \
                    rewards are calculated: {}",
                        LOG_PREFIX, id, e
                    );
                }
            };
            if is_over_instructions_limit() {
                break;
            }
        }
    }
```
