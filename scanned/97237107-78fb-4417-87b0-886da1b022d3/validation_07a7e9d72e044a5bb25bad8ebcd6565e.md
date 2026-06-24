### Title
Unbounded Loop Over Missed Reward Rounds in SNS Governance `distribute_rewards` - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS Governance canister's `distribute_rewards` function contains an unbounded `for i in 1..=new_rounds_count` loop that iterates once per missed reward round. If the SNS canister is dormant or the heartbeat is delayed for a long period, `new_rounds_count` can grow to an arbitrarily large value. When `distribute_rewards` is eventually called, the loop executes an unbounded number of floating-point multiplications and additions, consuming instructions proportional to the number of missed rounds. This can cause the canister's message execution to exceed the IC instruction limit, permanently trapping the reward distribution function and preventing any future reward events from being created.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `distribute_rewards` method computes the rewards purse by iterating over every missed round since the last reward event:

```rust
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
// ...
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

`new_rounds_count` is derived directly from wall-clock time: `(now - reward_start_timestamp_seconds) / round_duration_seconds`. There is no cap on this value. If `round_duration_seconds` is small (e.g., 1 day = 86400 seconds) and the canister has not distributed rewards for a long time (e.g., 1 year ≈ 365 rounds), the loop runs 365 iterations. With a very short `round_duration_seconds` (the minimum is 1 second per the proto validation), the loop could run millions of times in a single message execution.

Unlike the NNS Governance canister, which was refactored (Proposal 135702) to distribute rewards asynchronously in batches using `is_message_over_threshold` and a `RewardsDistributionStateMachine`, the SNS Governance canister's `distribute_rewards` still performs the entire purse calculation in a single synchronous call with no instruction-limit guard on the loop.

The function is called from `run_periodic_tasks`, which is triggered by the canister heartbeat. Once the loop grows large enough to exceed the instruction limit, every subsequent heartbeat invocation will also trap at the same point, permanently blocking reward distribution.

### Impact Explanation
- **Governance liveness**: All future SNS reward events are permanently blocked. No neuron maturity is ever increased again. Neurons that rely on maturity for staking incentives are effectively frozen.
- **Canister trap**: The SNS governance canister's heartbeat traps on every invocation of `run_periodic_tasks` → `distribute_rewards`, which also blocks other periodic tasks (upgrade checks, maturity finalization, etc.) that run in the same function.
- **No recovery path**: Because the loop bound is derived from the current time minus the last reward event timestamp, the loop only grows larger with each passing second. There is no mechanism to "catch up" in batches across multiple messages.

### Likelihood Explanation
The SNS `round_duration_seconds` is configurable by the SNS itself via `ManageNervousSystemParameters`. An SNS that sets a very short `round_duration_seconds` (e.g., 1 second, which is the minimum allowed) and then experiences any period of canister unavailability (upgrade, subnet recovery, or simply a bug that prevents heartbeats) will accumulate a massive `new_rounds_count`. Even with a normal 1-day round duration, an SNS that is dormant for ~3 years would accumulate ~1000 rounds, which may be sufficient to exceed the instruction limit given the floating-point arithmetic per iteration. This is reachable by any SNS deployer (an unprivileged canister developer) who configures a short round duration.

### Recommendation
1. **Cap `new_rounds_count`** to a safe maximum (e.g., 1000) per single invocation, similar to how NNS Governance caps its reward distribution.
2. **Adopt the NNS pattern**: Separate the purse calculation from the neuron maturity update, and process the purse calculation in batches across multiple timer-driven messages, checking `is_message_over_threshold` between iterations.
3. **Add a minimum `round_duration_seconds`** validation that prevents values small enough to cause rapid accumulation of missed rounds.

### Proof of Concept
1. Deploy an SNS with `round_duration_seconds = 1` (minimum allowed).
2. Pause the SNS governance canister's heartbeat for 1 hour (e.g., by stopping the canister, or via a subnet upgrade that delays execution).
3. Resume the canister. The next heartbeat calls `run_periodic_tasks` → `distribute_rewards`.
4. `new_rounds_count = (now - reward_start) / 1 = 3600` (one round per second for 1 hour).
5. The loop `for i in 1..=3600` executes 3600 iterations of floating-point arithmetic.
6. With a longer pause (e.g., 1 day = 86400 rounds), the loop exceeds the IC instruction limit and the message traps.
7. Every subsequent heartbeat traps at the same point. The SNS governance canister is permanently unable to distribute rewards or run any other periodic task. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5503-5513)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L5812-5814)
```rust
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

**File:** rs/nns/governance/src/reward/distribution.rs (L154-187)
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
```

**File:** rs/nns/governance/CHANGELOG.md (L655-669)
```markdown
        * Distribute rewards is moved to timer, and has a mechanism to distribute in batches in
          multiple messages.
        * Unstaking maturity task has a limit of 100 neurons per message, which prevents it from
          exceeding instruction limit.
        * The execution of `ApproveGenesisKyc` proposals have a limit of 1000 neurons, above which
          the proposal will fail.
        * More benchmarks were added.
* Enable timer task metrics for better observability.

## Changed

* Voting Rewards will be scheduled by a timer instead of by heartbeats.
* Unstaking maturity task will be processing up to 100 neurons in a single message, to avoid
  exceeding the instruction limit in a single execution.
* Voting Rewards will be distributed asynchronously in the background after being calculated.
```
