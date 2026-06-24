### Title
Unbounded `new_rounds_count` Loop in `distribute_rewards` Causes Permanent Instruction-Limit Trap - (`File: rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance canister's `distribute_rewards` function computes `new_rounds_count` as `(now - reward_start_timestamp_seconds) / round_duration_seconds`. For a freshly deployed SNS, `reward_start_timestamp_seconds` defaults to `0` (because `end_timestamp_seconds` is `None`), while `now` is the current Unix epoch (~1.7 × 10⁹ seconds). With the minimum-allowed `round_duration_seconds = 1`, the loop `for i in 1..=new_rounds_count` must iterate ~1.7 billion times. This exhausts the Wasm instruction limit, causing the canister to trap. Because the trap rolls back all state, `end_timestamp_seconds` is never written, so every subsequent periodic invocation of `distribute_rewards` encounters the same enormous `new_rounds_count` and traps again — permanently breaking SNS voting-reward distribution.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the number of missed reward rounds:

```rust
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();          // ← 0 for a new SNS
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
``` [1](#0-0) 

It then iterates over every missed round to accumulate the rewards purse:

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
``` [2](#0-1) 

`VotingRewardsParameters` validation accepts `round_duration_seconds` in the range `1..=MAX_REWARD_ROUND_DURATION_SECONDS`: [3](#0-2) 

With `round_duration_seconds = 1` (the minimum), `new_rounds_count ≈ now / 1 ≈ 1.7 × 10⁹`. The loop body performs multiple `rust_decimal::Decimal` multiplications and additions per iteration. At IC's ~5 × 10⁹ instruction budget per message, even a few hundred instructions per iteration exhausts the budget long before the loop completes. The canister traps, the state is rolled back, `end_timestamp_seconds` remains `None`, and the next periodic invocation of `distribute_rewards` faces the identical `new_rounds_count` — creating a permanent, self-reinforcing DoS.

### Impact Explanation

Voting-reward distribution in the SNS governance canister is permanently halted. Neurons that voted on proposals never receive maturity. The `latest_reward_event` is never updated, so `should_distribute_rewards` continues to return `true` and `distribute_rewards` is called on every heartbeat/timer tick, each time trapping and wasting cycles. The SNS governance canister remains otherwise functional (proposals can still be submitted and voted on), but the economic incentive layer is completely broken for the lifetime of the SNS.

### Likelihood Explanation

`round_duration_seconds = 1` is the minimum value accepted by `VotingRewardsParameters::validate`. An SNS developer who sets this value (e.g., for testing or for a high-frequency reward schedule) will trigger the bug on the very first reward distribution attempt. Even with `round_duration_seconds = 86400` (one day), `new_rounds_count ≈ 19,676` on a freshly deployed SNS whose first reward event has never fired — this is within the instruction budget today but will grow over time and could become problematic for SNSes with shorter round durations. The bug is latent from deployment and requires no external attacker: the SNS creator/developer is the entry point.

### Recommendation

Cap `new_rounds_count` to a safe maximum per single execution (e.g., 1,000 rounds), and carry forward any remaining rounds to the next invocation — analogous to how the NNS governance canister now distributes rewards in batches across multiple messages: [4](#0-3) 

Alternatively, enforce a tighter lower bound on `round_duration_seconds` during SNS initialization (e.g., minimum one hour) to prevent astronomically large `new_rounds_count` values from ever arising.

### Proof of Concept

1. Deploy an SNS with `VotingRewardsParameters { round_duration_seconds: Some(1), reward_rate_transition_duration_seconds: Some(0), initial_reward_rate_basis_points: Some(100), final_reward_rate_basis_points: Some(100) }`.
2. Wait for the first periodic task invocation of `distribute_rewards`.
3. `reward_start_timestamp_seconds = 0` (no prior `RewardEvent`).
4. `now ≈ 1_700_000_000` (current Unix epoch).
5. `new_rounds_count = 1_700_000_000 / 1 = 1_700_000_000`.
6. The loop at line 5861 attempts 1.7 billion iterations of `Decimal` arithmetic.
7. The canister traps with `CanisterInstructionLimitExceeded`.
8. State rolls back; `end_timestamp_seconds` remains `None`.
9. Every subsequent periodic invocation repeats steps 3–8 indefinitely.

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

**File:** rs/sns/governance/src/reward.rs (L254-259)
```rust
    fn round_duration_seconds_defects(&self) -> Vec<String> {
        require_field_set_and_in_range(
            "round_duration_seconds",
            &self.round_duration_seconds,
            1..=*MAX_REWARD_ROUND_DURATION_SECONDS,
        )
```

**File:** rs/nns/governance/src/reward/distribution.rs (L42-52)
```rust
    pub fn distribute_pending_rewards(&mut self) -> bool {
        let is_over_instructions_limit = || is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT);
        with_rewards_distribution_state_machine_mut(|rewards_distribution_state_machine| {
            rewards_distribution_state_machine.with_next_distribution(|(_, distribution)| {
                distribution
                    .continue_processing(&mut self.neuron_store, is_over_instructions_limit);
            });
            // Work left?
            !rewards_distribution_state_machine.distributions.is_empty()
        })
    }
```
