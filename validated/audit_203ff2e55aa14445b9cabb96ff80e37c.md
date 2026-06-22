### Title
Unbounded Loop Over Missed Reward Rounds in SNS `distribute_rewards` Can Exhaust Instruction Limit - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance `distribute_rewards` function contains an unbounded `for i in 1..=new_rounds_count` loop with no instruction-limit check. If `new_rounds_count` grows large — due to a small `round_duration_seconds` or a prolonged gap in reward distribution — the function exhausts the per-message instruction limit and traps. Because IC traps roll back state, `new_rounds_count` never decreases, permanently preventing reward distribution.

### Finding Description
`distribute_rewards` computes `new_rounds_count` as the number of reward rounds that have elapsed since the last distribution event:

```rust
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
```

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
```

There is no `is_over_instructions_limit()` guard inside this loop. [1](#0-0) 

`new_rounds_count` is unbounded: if `round_duration_seconds` is set to a small value (e.g., 1 second, which is configurable via SNS governance proposal), or if the canister is stopped/paused for an extended period, `new_rounds_count` can reach millions. Each iteration performs `Decimal` arithmetic via `reward_rate_at`, which is instruction-heavy. [2](#0-1) 

The function is called from `run_periodic_tasks` (the SNS heartbeat/timer path). When it traps, the IC rolls back all state changes, so `latest_reward_event` is never updated, `new_rounds_count` never decreases, and every subsequent invocation also traps. [3](#0-2) 

By contrast, the NNS governance equivalent (`calculate_voting_rewards`) iterates over `days` using a simple `f64` sum — far cheaper per iteration — and the NNS reward distribution itself was refactored to use a batched, instruction-aware state machine. [4](#0-3) 

The SNS `distribute_rewards` also contains two additional unbounded loops after the purse calculation — one over all `considered_proposals` × their `ballots`, and one over all `neuron_id_to_reward_shares` — neither of which has an instruction-limit check. [5](#0-4) 

### Impact Explanation
A permanently stuck `distribute_rewards` means:
- All SNS neurons stop receiving voting rewards indefinitely.
- The SNS governance canister's periodic task loop is disrupted on every heartbeat/timer tick.
- The SNS cannot self-recover without an upgrade that resets `latest_reward_event` or caps `new_rounds_count`.

This is a cycles/resource accounting bug causing a permanent DoS on the SNS reward subsystem.

### Likelihood Explanation
Two realistic trigger paths exist:

1. **Small `round_duration_seconds`**: An SNS governance proposal sets `round_duration_seconds` to a very small value (e.g., 1 second). After even a few hours, `new_rounds_count` reaches tens of thousands. This requires a governance majority, but SNS governance majorities are achievable by coordinated token holders.

2. **Extended canister pause**: If the SNS governance canister is stopped or fails to execute its timer for an extended period (e.g., due to a subnet issue, upgrade, or cycles exhaustion), `new_rounds_count` accumulates. When the canister resumes, the first `distribute_rewards` call traps, and the canister is permanently stuck.

Path 2 requires no privileged action — any subnet-level disruption or canister upgrade gap can trigger it.

### Recommendation
Apply the same pattern used in NNS governance reward distribution:
1. Cap `new_rounds_count` to a safe maximum per invocation (e.g., 100 rounds), and carry over the remainder to the next timer tick by updating `latest_reward_event` incrementally.
2. Add an `is_over_instructions_limit()` guard inside the `for i in 1..=new_rounds_count` loop, breaking early and persisting progress.
3. Add similar guards to the ballot-iteration and neuron-reward-distribution loops that follow.

### Proof of Concept
1. Deploy an SNS with `round_duration_seconds = 1`.
2. Wait 24 hours. `new_rounds_count` = 86,400.
3. Trigger any update call that invokes `run_periodic_tasks` → `distribute_rewards`.
4. The `for i in 1..=86400` loop, each iteration calling `reward_rate_at` (Decimal arithmetic), exhausts the ~5 billion instruction limit and traps.
5. State rolls back; `latest_reward_event` is unchanged; `new_rounds_count` remains 86,400+.
6. Every subsequent heartbeat/timer tick repeats step 4. Voting rewards are permanently frozen. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5763-5765)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
        let now = self.env.now();
```

**File:** rs/sns/governance/src/governance.rs (L5812-5814)
```rust
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
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

**File:** rs/sns/governance/src/governance.rs (L5894-5997)
```rust
        for proposal_id in &considered_proposals {
            if let Some(proposal) = self.get_proposal_data(*proposal_id) {
                for (voter, ballot) in &proposal.ballots {
                    #[allow(clippy::blocks_in_conditions)]
                    if !Vote::try_from(ballot.vote)
                        .unwrap_or_else(|_| {
                            println!(
                                "{}Vote::from invoked with unexpected value {}.",
                                log_prefix(),
                                ballot.vote
                            );
                            Vote::Unspecified
                        })
                        .eligible_for_rewards()
                    {
                        continue;
                    }

                    match NeuronId::from_str(voter) {
                        Ok(neuron_id) => {
                            let reward_shares = i2d(ballot.voting_power);
                            *neuron_id_to_reward_shares
                                .entry(neuron_id)
                                .or_insert_with(|| dec!(0)) += reward_shares;
                        }
                        Err(e) => {
                            log!(
                                ERROR,
                                "Could not use voter {} to calculate total_voting_rights \
                                 since it's NeuronId was invalid. Underlying error: {:?}.",
                                voter,
                                e
                            );
                        }
                    }
                }
            }
        }
        // Freeze reward shares, now that we are done adding them up.
        let neuron_id_to_reward_shares = neuron_id_to_reward_shares;
        let total_reward_shares: Decimal = neuron_id_to_reward_shares.values().sum();
        debug_assert!(
            total_reward_shares >= dec!(0),
            "total_reward_shares: {total_reward_shares} neuron_id_to_reward_shares: {neuron_id_to_reward_shares:#?}",
        );

        // Because of rounding (and other shenanigans), it is possible that some
        // portion of this amount ends up not being actually distributed.
        let mut distributed_e8s_equivalent = 0_u64;
        // Now that we know the size of the pie (rewards_purse_e8s), and how
        // much of it each neuron is supposed to get (*_reward_shares), we now
        // proceed to actually handing out those rewards.
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
        } else {
            for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
                let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) {
                    Ok(neuron) => neuron,
                    Err(err) => {
                        log!(
                            ERROR,
                            "Cannot find neuron {}, despite having voted with power {} \
                             in the considered reward period. The reward that should have been \
                             distributed to this neuron is simply skipped, so the total amount \
                             of distributed reward for this period will be lower than the maximum \
                             allowed. Underlying error: {:?}.",
                            neuron_id,
                            neuron_reward_shares,
                            err
                        );
                        continue;
                    }
                };

                // Dividing before multiplying maximizes our chances of success.
                let neuron_reward_e8s =
                    rewards_purse_e8s * (neuron_reward_shares / total_reward_shares);

                // Round down, and convert to u64.
                let neuron_reward_e8s = u64::try_from(neuron_reward_e8s).unwrap_or_else(|err| {
                    panic!(
                        "Calculating reward for neuron {neuron_id:?}:\n\
                             neuron_reward_shares: {neuron_reward_shares}\n\
                             rewards_purse_e8s: {rewards_purse_e8s}\n\
                             total_reward_shares: {total_reward_shares}\n\
                             err: {err}",
                    )
                });
                // If the neuron has auto-stake-maturity on, add the new maturity to the
                // staked maturity, otherwise add it to the un-staked maturity.
                if neuron.auto_stake_maturity.unwrap_or(false) {
                    neuron.staked_maturity_e8s_equivalent = Some(
                        neuron.staked_maturity_e8s_equivalent.unwrap_or(0) + neuron_reward_e8s,
                    );
                } else {
                    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
                }
                distributed_e8s_equivalent += neuron_reward_e8s;
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
