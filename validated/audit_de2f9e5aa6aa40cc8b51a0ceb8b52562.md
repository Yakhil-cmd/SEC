Audit Report

## Title
Unbounded `new_rounds_count` Loop in `distribute_rewards` Causes Permanent Instruction-Limit Trap - (File: rs/sns/governance/src/governance.rs)

## Summary
The SNS governance canister's `distribute_rewards` function computes `new_rounds_count` as `(now - reward_start_timestamp_seconds) / round_duration_seconds`, where `reward_start_timestamp_seconds` defaults to `0` via `unwrap_or_default()` when no prior reward event exists. With `round_duration_seconds = 1` (the minimum accepted by validation), this yields ~1.7 billion loop iterations of `rust_decimal::Decimal` arithmetic, exhausting the Wasm instruction budget. Because the trap rolls back all state, `end_timestamp_seconds` is never written, and every subsequent periodic invocation of `distribute_rewards` encounters the same enormous `new_rounds_count`, permanently halting SNS voting-reward distribution.

## Finding Description
In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();          // 0 for a new SNS
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
``` [1](#0-0) 

It then iterates over every missed round with no cap:

```rust
for i in 1..=new_rounds_count {
    ...
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
``` [2](#0-1) 

The only existing guard checks `round_duration_seconds == 0` (division-by-zero) and `new_rounds_count == 0` (no-op), but there is no upper bound on `new_rounds_count`. [3](#0-2) 

`VotingRewardsParameters` validation explicitly accepts `round_duration_seconds` in `1..=MAX_REWARD_ROUND_DURATION_SECONDS`, where `MAX_REWARD_ROUND_DURATION_SECONDS` is ~1 year in seconds and the minimum is `1`: [4](#0-3) [5](#0-4) 

With `round_duration_seconds = 1` and no prior reward event (`end_timestamp_seconds = None → 0`), `new_rounds_count ≈ now / 1 ≈ 1.7 × 10⁹`. The loop body performs multiple `Decimal` multiplications and additions per iteration. At IC's ~5 × 10⁹ instruction budget per message, even a few hundred instructions per iteration exhausts the budget before the loop completes. The canister traps, state rolls back, `end_timestamp_seconds` remains `None`, and the next periodic invocation faces the identical `new_rounds_count` — a permanent, self-reinforcing DoS. No cap on `new_rounds_count` exists anywhere in the SNS governance codebase.

## Impact Explanation
Voting-reward distribution in the SNS governance canister is permanently halted. Neurons that voted on proposals never receive maturity. `latest_reward_event` is never updated, so `should_distribute_rewards` continues returning `true` and `distribute_rewards` is called on every heartbeat/timer tick, each time trapping and wasting cycles. This constitutes a significant SNS security impact with concrete, permanent user harm — matching the allowed High impact: *"Significant SNS infrastructure security impact with concrete user or protocol harm."*

## Likelihood Explanation
`round_duration_seconds = 1` is the minimum value accepted by `VotingRewardsParameters::validate`. Any SNS deployer (the SNS framework is permissionless) who sets this value — for testing, high-frequency rewards, or inadvertently — triggers the bug on the very first reward distribution attempt. No external attacker is required; the SNS creator is the sole entry point. Even with `round_duration_seconds = 3600` (one hour), `new_rounds_count ≈ 472,222` on a freshly deployed SNS, which may still exhaust the instruction budget given the `Decimal` arithmetic cost per iteration.

## Recommendation
Cap `new_rounds_count` to a safe maximum per single execution (e.g., 1,000 rounds) and carry forward remaining rounds to the next invocation, analogous to how the NNS governance canister distributes rewards in batches using `distribute_pending_rewards` with an instruction-limit check. Alternatively, enforce a tighter lower bound on `round_duration_seconds` during SNS initialization (e.g., minimum one hour, `3600`) to prevent astronomically large `new_rounds_count` values from arising with valid configurations.

## Proof of Concept
1. Deploy an SNS with `VotingRewardsParameters { round_duration_seconds: Some(1), reward_rate_transition_duration_seconds: Some(0), initial_reward_rate_basis_points: Some(100), final_reward_rate_basis_points: Some(100) }`.
2. Wait for the first periodic task invocation of `distribute_rewards`.
3. `reward_start_timestamp_seconds = 0` (no prior `RewardEvent`, `unwrap_or_default()`).
4. `now ≈ 1_700_000_000` (current Unix epoch).
5. `new_rounds_count = 1_700_000_000 / 1 = 1_700_000_000`.
6. The loop at line 5861 attempts 1.7 billion iterations of `Decimal` arithmetic.
7. The canister traps with `CanisterInstructionLimitExceeded`.
8. State rolls back; `end_timestamp_seconds` remains `None`.
9. Every subsequent periodic invocation repeats steps 3–8 indefinitely.

A deterministic integration test using PocketIC can reproduce this by setting `round_duration_seconds = 1`, advancing the mock clock to a realistic Unix timestamp, and asserting that `distribute_rewards` traps and that `latest_reward_event().end_timestamp_seconds` remains `None` after the call.

### Citations

**File:** rs/sns/governance/src/governance.rs (L5796-5820)
```rust
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

**File:** rs/sns/governance/src/reward.rs (L44-46)
```rust
    pub static ref MAX_REWARD_ROUND_DURATION_SECONDS: u64 =
        u64::try_from(*NOMINAL_DAYS_PER_YEAR * *ONE_DAY_SECONDS)
            .expect("Unable to convert a Decimal into a u64.");
```

**File:** rs/sns/governance/src/reward.rs (L254-259)
```rust
    fn round_duration_seconds_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "round_duration_seconds",
            &self.round_duration_seconds,
            1..=*MAX_REWARD_ROUND_DURATION_SECONDS,
        )
```
