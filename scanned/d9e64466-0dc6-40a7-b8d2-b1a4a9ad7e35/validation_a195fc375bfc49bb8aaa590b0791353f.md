### Title
SNS Governance Reward Rate Evaluated at Round-End Instead of Round-Start, Causing Systematic Reward Underestimation - (File: rs/sns/governance/src/governance.rs)

### Summary
In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function evaluates the voting reward rate at the **end** of each reward round rather than the **beginning**. Because the reward rate decreases monotonically over time (from `initial_reward_rate_basis_points` toward `final_reward_rate_basis_points`), this off-by-one epoch boundary error causes every SNS to systematically underpay voting rewards to neuron holders on every reward round throughout the entire rate-transition period.

### Finding Description

In `distribute_rewards`, the rewards purse is computed by looping over each elapsed round and calling `reward_rate_at` with a timestamp derived from the loop index `i` (1-indexed):

```rust
for i in 1..=new_rounds_count {
    let seconds_since_genesis = round_duration_seconds
        .saturating_mul(i)                          // <-- uses END of round i
        .saturating_add(reward_start_timestamp_seconds)
        .saturating_sub(self.proto.genesis_timestamp_seconds);

    let current_reward_rate = voting_rewards_parameters.reward_rate_at(
        crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
    );
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
```

For `i = 1`, `seconds_since_genesis` resolves to `reward_start_timestamp_seconds + round_duration_seconds - genesis_timestamp_seconds`, which is the **end** of round 1 (equivalently, the start of round 2). The reward rate is therefore sampled one full round duration into the future relative to the round being paid.

The NNS governance explicitly documents and implements the opposite convention — sampling the rate at the **beginning** of each period:

```rust
// Despite the rate being arguable a continuous function of time, we don't
// integrate the rate here. Instead we multiply the rate at the beginning of
// that day with the considered duration.
``` [1](#0-0) [2](#0-1) 

### Impact Explanation

`reward_rate_at` implements a quadratic decay from `initial_reward_rate_basis_points` to `final_reward_rate_basis_points` over `reward_rate_transition_duration_seconds`:

```rust
let s = transition.apply(time_since_genesis.as_secs()); // 1→0 linearly
let s2 = s * s;                                          // quadratic
let variable_reward_rate = s2 * dr;
self.final_reward_rate() + variable_reward_rate
``` [3](#0-2) 

Because the rate is strictly decreasing during the transition period, sampling at the end of each round instead of the beginning always yields a lower rate. The per-round error is:

```
ΔR ≈ (initial_rate − final_rate) × 2 × (round_duration / transition_duration)
```

For a typical SNS (10 % → 5 % over 8 years, 1-day rounds, 1 B token supply):
- Per-round error ≈ ~9 tokens/day
- Cumulative error over 8 years ≈ tens of thousands of tokens

The error is permanent and irrecoverable: once a reward event is recorded, the underpaid maturity is never corrected. All SNS neuron holders who voted during the transition period receive less maturity than the protocol intends. [4](#0-3) 

### Likelihood Explanation

**High.** The bug fires unconditionally on every call to `distribute_rewards` during the rate-transition period. `distribute_rewards` is invoked automatically by `run_periodic_tasks` on every SNS governance canister. No special conditions, attacker actions, or unusual configurations are required. Any SNS with `initial_reward_rate_basis_points > final_reward_rate_basis_points` and a non-zero `reward_rate_transition_duration_seconds` is affected from its first reward round onward. [5](#0-4) 

### Recommendation

Change the loop to sample the reward rate at the **start** of each round by substituting `i - 1` for `i` in the `seconds_since_genesis` calculation:

```rust
for i in 1..=new_rounds_count {
    let seconds_since_genesis = round_duration_seconds
        .saturating_mul(i - 1)   // sample at the START of round i
        .saturating_add(reward_start_timestamp_seconds)
        .saturating_sub(self.proto.genesis_timestamp_seconds);
    ...
}
```

This aligns SNS behavior with the NNS convention (rate sampled at the beginning of the period) and with the mathematical intent of the simple-interest formula `principal × rate × duration`, where `rate` should reflect the rate in force at the opening of the period being paid. [2](#0-1) 

### Proof of Concept

**Setup:** Deploy an SNS with:
- `initial_reward_rate_basis_points = 1000` (10 %)
- `final_reward_rate_basis_points = 500` (5 %)
- `reward_rate_transition_duration_seconds = 252_288_000` (8 years)
- `round_duration_seconds = 86_400` (1 day)
- Token supply = 1,000,000,000 e8s

**Round 1 — current (buggy) behavior:**
- `seconds_since_genesis` = `86_400 * 1 + 0 - 0` = `86_400` (end of round 1)
- `reward_rate_at(86_400 s)` ≈ 9.99966 % per year (slightly below initial)
- Reward purse ≈ `1e9 × 0.0999966 / 365.25` ≈ 273,775 e8s

**Round 1 — correct behavior (rate at start):**
- `seconds_since_genesis` = `86_400 * 0 + 0 - 0` = `0` (start of round 1)
- `reward_rate_at(0)` = 10.0 % per year (initial rate)
- Reward purse ≈ `1e9 × 0.10 / 365.25` ≈ 273,785 e8s

**Shortfall per round ≈ 10 e8s.** Multiplied across all rounds in the 8-year transition period (≈ 2,922 rounds), the cumulative underpayment is on the order of tens of thousands of tokens — a permanent, irrecoverable loss of maturity for all participating neuron holders. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5808-5812)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
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

**File:** rs/nns/governance/src/reward/calculation.rs (L82-87)
```rust
    // Despite the rate being arguable a continuous function of time, we don't
    // integrate the rate here. Instead we multiply the rate at the beginning of
    // that day with the considered duration.
    let t = IcTimestamp {
        days_since_ic_genesis: days_since_ic_genesis as f64,
    };
```

**File:** rs/sns/governance/src/reward.rs (L197-217)
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
```

**File:** rs/sns/governance/src/reward.rs (L219-241)
```rust
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
```
