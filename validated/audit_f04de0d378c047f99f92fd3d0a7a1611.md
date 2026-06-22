### Title
Off-by-One Reward Rate Epoch in SNS `distribute_rewards` Causes Systematic Under-Rewarding of Neuron Holders - (File: rs/sns/governance/src/governance.rs)

---

### Summary

The SNS governance `distribute_rewards` function computes the reward rate for each round using the timestamp at the **end** of the round rather than the **beginning**. Because the SNS reward rate is monotonically decreasing over time, this off-by-one epoch selection systematically underestimates the reward purse for every round, causing all SNS neuron holders to receive less voting maturity than they are entitled to.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` function accumulates the reward purse over `new_rounds_count` rounds with the following loop:

```rust
for i in 1..=new_rounds_count {
    let seconds_since_genesis = round_duration_seconds
        .saturating_mul(i)                          // ← uses END of round i
        .saturating_add(reward_start_timestamp_seconds)
        .saturating_sub(self.proto.genesis_timestamp_seconds);

    let current_reward_rate = voting_rewards_parameters.reward_rate_at(
        crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
    );

    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
``` [1](#0-0) 

For round `i`, the timestamp fed to `reward_rate_at` is:

```
reward_start_timestamp_seconds + i × round_duration_seconds − genesis_timestamp_seconds
```

This is the **end** of round `i`. The correct timestamp for the rate that was in effect **during** round `i` is the **beginning** of round `i`:

```
reward_start_timestamp_seconds + (i−1) × round_duration_seconds − genesis_timestamp_seconds
```

The NNS governance code makes this design intent explicit in its parallel implementation:

> "Despite the rate being arguable a continuous function of time, we don't integrate the rate here. Instead we multiply the **rate at the beginning of that day** with the considered duration." [2](#0-1) 

The SNS `reward_rate_at` function returns a rate that decreases monotonically from `initial_reward_rate_basis_points` toward `final_reward_rate_basis_points` over `reward_rate_transition_duration_seconds`: [3](#0-2) 

Because the rate at the end of a round is always ≤ the rate at the beginning of that round, every round's contribution to `rewards_purse_e8s` is understated. The accumulated purse is then distributed proportionally to neuron voting shares: [4](#0-3) 

---

### Impact Explanation

Every SNS neuron holder who participates in governance voting receives less maturity than they are entitled to. The shortfall per round equals:

```
[rate(round_end) − rate(round_start)] × round_duration × token_supply
```

Since `rate(round_end) < rate(round_start)` throughout the transition period, the shortfall is always negative (i.e., users always receive less). The error is systematic and accumulates across every reward round for the entire `reward_rate_transition_duration_seconds` window. For SNS instances configured with a large rate spread (e.g., `initial = 10 000 bp`, `final = 0 bp`) and a short transition period relative to round duration, the per-round underestimation can be material. The lost maturity is never recovered — it is simply not minted.

---

### Likelihood Explanation

This code path executes on every SNS governance canister that has `voting_rewards_parameters` set with a non-zero `reward_rate_transition_duration_seconds`. It is triggered automatically by the canister's periodic task (`run_periodic_tasks`) without any privileged action. Any unprivileged governance participant (neuron holder) who votes causes proposals to enter `ReadyToSettle`, which in turn causes `distribute_rewards` to be invoked. No special role, key, or majority is required to reach this code. [5](#0-4) 

---

### Recommendation

Change the loop to use `i.saturating_sub(1)` (i.e., the **beginning** of round `i`) when computing `seconds_since_genesis`:

```rust
for i in 1..=new_rounds_count {
    let seconds_since_genesis = round_duration_seconds
        .saturating_mul(i - 1)   // rate at the START of round i
        .saturating_add(reward_start_timestamp_seconds)
        .saturating_sub(self.proto.genesis_timestamp_seconds);

    let current_reward_rate = voting_rewards_parameters.reward_rate_at(
        crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
    );

    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
```

This aligns the SNS implementation with the NNS design intent documented in `rs/nns/governance/src/reward/calculation.rs`. [6](#0-5) 

---

### Proof of Concept

Consider an SNS with:
- `initial_reward_rate_basis_points = 200` (2 %/yr)
- `final_reward_rate_basis_points = 100` (1 %/yr)
- `reward_rate_transition_duration_seconds = 42 × 7 × 86400` (42 weeks)
- `round_duration_seconds = 7 × 86400` (1 week)
- Token supply = 1 000 000 e8s

For round 1 (`i = 1`):
- **Correct** `seconds_since_genesis` = 0 → rate = 2 %/yr → reward ≈ 2 000 000 × (2/100) × (7/365.25) e8s
- **Actual** `seconds_since_genesis` = 604 800 → rate ≈ 1.976 %/yr (slightly lower) → reward is understated

The shortfall per round ≈ 0.024 % of the round reward. Across 42 rounds (the full transition), the cumulative shortfall is non-trivial and is never compensated. The `reward_rate_at` function and its test suite confirm the rate is strictly decreasing during the transition: [7](#0-6)

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

**File:** rs/sns/governance/src/governance.rs (L5973-5975)
```rust
                // Dividing before multiplying maximizes our chances of success.
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);
```

**File:** rs/nns/governance/src/reward/calculation.rs (L79-101)
```rust
pub fn rewards_pool_to_distribute_in_supply_fraction_for_one_day(
    days_since_ic_genesis: u64,
) -> f64 {
    // Despite the rate being arguable a continuous function of time, we don't
    // integrate the rate here. Instead we multiply the rate at the beginning of
    // that day with the considered duration.
    let t = IcTimestamp {
        days_since_ic_genesis: days_since_ic_genesis as f64,
    };

    let variable_rate = if t > REWARD_FLATTENING_DATE {
        InverseDuration { per_day: 0.0 }
    } else {
        let duration_to_bottom = t - REWARD_FLATTENING_DATE;
        let closeness_to_bottom = duration_to_bottom / (GENESIS - REWARD_FLATTENING_DATE);
        let delta_rate = INITIAL_VOTING_REWARD_RELATIVE_RATE - FINAL_VOTING_REWARD_RELATIVE_RATE;
        closeness_to_bottom.powf(2.0) * delta_rate
    };

    let rate = FINAL_VOTING_REWARD_RELATIVE_RATE + variable_rate;

    voting_rewards_adjustment_factor() * rate * ONE_DAY
}
```

**File:** rs/sns/governance/src/reward.rs (L197-241)
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
```

**File:** rs/sns/governance/src/reward.rs (L556-572)
```rust
    #[test]
    fn reward_is_bounded_during_transition() {
        let r_i = VOTING_REWARDS_PARAMETERS.initial_reward_rate();
        let r_f = VOTING_REWARDS_PARAMETERS.final_reward_rate();

        for round in 2..TRANSITION_ROUND_COUNT {
            let reward_rate =
                VOTING_REWARDS_PARAMETERS.reward_rate_at(round_number_to_instant(round));
            assert!(
                reward_rate < r_i,
                "round = {round}, r_i = {r_i:#?}, reward_rate = {reward_rate:#?}",
            );
            assert!(
                reward_rate > r_f,
                "round = {round}, r_f = {r_f:#?}, reward_rate = {reward_rate:#?}",
            );
        }
```
