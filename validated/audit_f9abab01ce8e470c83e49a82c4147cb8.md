### Title
SNS Governance `distribute_rewards` Unbounded Nested Loop Can Exceed Instruction Limit — (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance `distribute_rewards` function contains nested loops over missed reward rounds, proposals, ballots, and neurons with no instruction-limit guard or batching mechanism. Unlike NNS governance (which was explicitly updated in Proposal 135702 to distribute rewards asynchronously in batches via a timer), the SNS governance still processes all reward work in a single heartbeat execution. If the SNS accumulates many missed rounds, many proposals in `ReadyToSettle` state, and many neurons, the heartbeat can exceed the IC instruction limit, permanently blocking reward distribution.

### Finding Description
The `distribute_rewards` function in `rs/sns/governance/src/governance.rs`, called from `run_periodic_tasks` on every heartbeat, contains the following nested computation:

**Loop 1 — missed rounds** (line 5861):
```rust
for i in 1..=new_rounds_count {
    // Decimal arithmetic per round
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
```
`new_rounds_count` is unbounded: it equals `(now - last_reward_event_end) / round_duration_seconds`. If the canister was unavailable for any period (upgrade, subnet issue), this grows proportionally to elapsed time. [1](#0-0) 

**Loop 2 — proposals × ballots** (lines 5894–5930):
```rust
for proposal_id in &considered_proposals {
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots {
            // HashMap insert per ballot
        }
    }
}
```
`considered_proposals` contains all proposals in `ReadyToSettle` state. Each proposal's `ballots` map has one entry per neuron that voted. [2](#0-1) 

**Loop 3 — neurons** (line 5954):
```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) { ... };
    // Decimal division + neuron mutation per neuron
}
``` [3](#0-2) 

Total work is **O(R + P × N + N)** where R = missed rounds, P = proposals in ReadyToSettle, N = neurons. There is no `is_message_over_threshold` check, no batching, and no pagination anywhere in this path.

By contrast, the NNS governance explicitly fixed this pattern. The NNS CHANGELOG for Proposal 135702 states:
> "Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages."
> "Unstaking maturity task has a limit of 100 neurons per message, which prevents it from exceeding instruction limit." [4](#0-3) 

The NNS fix uses `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` checked after each neuron in `continue_processing`, breaking work across multiple timer firings: [5](#0-4) 

The SNS governance has no equivalent protection.

### Impact Explanation
If the instruction limit is exceeded during the heartbeat, the entire execution is rolled back with `CanisterInstructionLimitExceeded`. The `latest_reward_event` is not updated, so the next heartbeat will attempt the same computation again — and fail again. This creates a **permanent liveness failure**: voting rewards are never distributed to SNS neurons. Neurons that voted on proposals will never receive their maturity rewards. The SNS reward mechanism is permanently broken until a canister upgrade is deployed.

### Likelihood Explanation
The conditions are reachable through normal SNS governance participation:

1. **Missed rounds**: Any canister upgrade, subnet maintenance, or temporary unavailability causes `new_rounds_count` to accumulate. With `round_duration_seconds = 86400` (1 day), a 30-day outage yields R = 30.
2. **Proposals × neurons**: SNS governance participants can create proposals (up to the configured limit) and vote on them. With 50 proposals each voted on by 10,000 neurons, the ballot loop runs 500,000 times.
3. **Compounding**: R × (P × N) work in a single message. At ~100–500 instructions per Decimal arithmetic operation, 500,000 ballot iterations with Decimal math approaches the 5 billion instruction limit on application subnets.

The NNS governance team explicitly identified this as a real risk and fixed it. The SNS governance has not received the same fix, making this a known-class vulnerability in an unpatched component.

### Recommendation
Apply the same batching mechanism used in NNS governance:
1. Move `distribute_rewards` to a dedicated timer task (not the heartbeat).
2. Persist intermediate `neuron_id_to_reward_shares` state across messages using stable storage.
3. Check `is_message_over_threshold` after each neuron update and break early, resuming in the next timer firing.
4. Replace the `for i in 1..=new_rounds_count` loop with a closed-form sum for the rewards purse calculation to avoid O(R) per-round iteration.

### Proof of Concept
1. Deploy an SNS with `round_duration_seconds = 86400` (1 day) and `max_number_of_proposals_with_ballots_to_settle = 50`.
2. Create 10,000 neurons and have them vote on 50 proposals (500,000 ballots total).
3. Stop the SNS governance canister for 30 days (simulating a subnet upgrade or canister stop), then restart it.
4. The heartbeat calls `run_periodic_tasks` → `distribute_rewards` with `new_rounds_count = 30`.
5. The function executes: 30 Decimal-arithmetic iterations (rounds purse) + 500,000 HashMap insertions (ballots) + 10,000 neuron mutations with Decimal division.
6. On an application subnet with a 5B instruction limit, this exceeds the limit. The heartbeat fails with `CanisterInstructionLimitExceeded`, state rolls back, `latest_reward_event` is unchanged.
7. Every subsequent heartbeat repeats the same failing computation. Reward distribution is permanently broken.

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L5894-5930)
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
```

**File:** rs/sns/governance/src/governance.rs (L5954-5997)
```rust
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
