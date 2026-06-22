### Title
Unbounded Reward-Round Loop in SNS Governance `distribute_rewards` Exhausts Instruction Limit on First Invocation - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance `distribute_rewards` function iterates `new_rounds_count` times in an unbounded loop to accumulate the rewards purse. `new_rounds_count` is derived from `(now − reward_start_timestamp_seconds) / round_duration_seconds`. When an SNS is initialised with the minimum-allowed `round_duration_seconds = 1` second, and the initial `latest_reward_event.end_timestamp_seconds` is `None` (defaulting to 0 via `unwrap_or_default()`), `new_rounds_count` equals the current Unix timestamp (~1.7 billion). The resulting loop exhausts the canister instruction limit on the very first heartbeat that calls `distribute_rewards`, permanently trapping every subsequent periodic-task execution and freezing reward distribution for all neuron holders.

### Finding Description
`distribute_rewards` in `rs/sns/governance/src/governance.rs` computes the number of elapsed reward rounds as:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();          // → 0 when no event has been recorded yet
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
``` [1](#0-0) 

It then iterates unconditionally over every elapsed round:

```rust
for i in 1..=new_rounds_count {
    ...
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
``` [2](#0-1) 

The `VotingRewardsParameters` validation accepts `round_duration_seconds` in the range `1..=MAX_REWARD_ROUND_DURATION_SECONDS` (≈ 31 557 600 s, one year):

```rust
fn round_duration_seconds_defects(&self) -> Vec<String> {
    require_field_set_and_in_range(
        "round_duration_seconds",
        &self.round_duration_seconds,
        1..=*MAX_REWARD_ROUND_DURATION_SECONDS,
    )
}
``` [3](#0-2) 

A freshly deployed SNS has `latest_reward_event.end_timestamp_seconds = None`. The `unwrap_or_default()` call returns `0`, so `reward_start_timestamp_seconds = 0`. With `round_duration_seconds = 1`, `new_rounds_count ≈ now ≈ 1 700 000 000`. Each loop body performs several `Decimal` floating-point multiplications (hundreds of Wasm instructions each). The total instruction count far exceeds the IC per-message limit (~5 billion instructions), causing the heartbeat to trap on every invocation.

### Impact Explanation
The heartbeat calls `run_periodic_tasks`, which calls `distribute_rewards`. Because the trap rolls back state, `latest_reward_event` is never updated, so `reward_start_timestamp_seconds` remains 0 on the next heartbeat. The canister is permanently stuck: every heartbeat traps before completing `distribute_rewards`, blocking all other periodic tasks (proposal processing, maturity spawning, etc.) that share the same `run_periodic_tasks` call. No neuron ever receives voting rewards; the SNS governance canister's liveness is degraded for all token holders.

### Likelihood Explanation
An SNS developer/deployer who sets `round_duration_seconds = 1` (the minimum value that passes validation) triggers the condition immediately on the first heartbeat after deployment. The deployer is a "canister caller/developer" entry point explicitly listed in scope. The configuration passes all on-chain validation checks, so no privileged override or governance majority is required — only the initial SNS init payload. The scenario is realistic for a developer testing short reward cycles or for a malicious SNS launcher targeting token holders.

### Recommendation
Cap `new_rounds_count` to a safe maximum before entering the loop (e.g., `new_rounds_count.min(MAX_ROUNDS_PER_DISTRIBUTION)`), and process remaining rounds in subsequent heartbeat invocations. Alternatively, replace the per-round loop with a closed-form calculation of the total reward purse (as the NNS governance does with a single `days.map(...).sum()` over a bounded day range), eliminating the O(n) iteration entirely.

### Proof of Concept
```rust
// SNS init payload
let params = NervousSystemParameters {
    voting_rewards_parameters: Some(VotingRewardsParameters {
        round_duration_seconds: Some(1),   // minimum valid value
        reward_rate_transition_duration_seconds: Some(0),
        initial_reward_rate_basis_points: Some(100),
        final_reward_rate_basis_points: Some(100),
    }),
    ..NervousSystemParameters::with_default_values()
};
// After deployment:
//   latest_reward_event.end_timestamp_seconds = None  →  reward_start = 0
//   now ≈ 1_700_000_000
//   new_rounds_count = 1_700_000_000 / 1 = 1_700_000_000
//
// The loop `for i in 1..=1_700_000_000` exhausts the instruction limit.
// Every subsequent heartbeat traps identically; state never advances.
```

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
