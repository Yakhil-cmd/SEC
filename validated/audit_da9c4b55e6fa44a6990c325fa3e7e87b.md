### Title
Unbounded Loops in SNS Governance `distribute_rewards` Can Permanently Block Voting Reward Distribution - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister's `distribute_rewards` function contains multiple unbounded loops with no instruction-limit checks. If the number of missed reward rounds or the number of voting neurons grows large enough to exhaust the IC instruction limit in a single message execution, the canister traps, all state changes are rolled back, and the next periodic invocation faces the same (or larger) workload — permanently blocking voting reward distribution for all SNS neurons.

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` is called synchronously from `run_periodic_tasks` and contains three nested, unbounded loops with no instruction-limit guard:

**Loop 1 — over missed reward rounds (`new_rounds_count`):** [1](#0-0) 

`new_rounds_count` is computed as `(now - reward_start_timestamp_seconds) / round_duration_seconds`. If the SNS governance canister misses reward distribution for any reason (upgrade, bug, or a very small `round_duration_seconds` configured at SNS creation), this value can grow arbitrarily large. There is no cap and no instruction-limit check inside the loop.

**Loop 2 — over all proposals and all their ballots:** [2](#0-1) 

For each proposal in `considered_proposals`, the code iterates over every ballot. With `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` proposals each carrying ballots for every neuron, this is O(proposals × neurons) work with no instruction-limit break.

**Loop 3 — over all rewarded neurons:** [3](#0-2) 

Iterates over every neuron that voted, updating maturity in-place, with no instruction-limit check.

**Contrast with NNS governance:** The NNS governance canister was explicitly updated to use a batched, timer-based `RewardsDistributionStateMachine` with per-iteration instruction-limit checks via `is_message_over_threshold`: [4](#0-3) 

The SNS governance has no equivalent mechanism. The NNS CHANGELOG explicitly documents this fix: [5](#0-4) 

The SNS `distribute_rewards` is called directly and synchronously: [6](#0-5) 

### Impact Explanation

If any of the three loops exhausts the IC instruction limit, the canister traps. On IC, a trap rolls back all state changes within that message execution. Because `latest_reward_event` is only written at the very end of `distribute_rewards`, a trap leaves it unchanged. The next invocation of `run_periodic_tasks` will compute the same or a larger `new_rounds_count`, triggering the same trap again. This creates a **permanent, self-reinforcing denial of service** for voting reward distribution: no neuron in the SNS can ever receive maturity rewards again, and the condition cannot be resolved without a canister upgrade.

### Likelihood Explanation

The trigger condition is realistic:

1. **Small `round_duration_seconds`**: An SNS can be configured with a short reward round. If the SNS governance canister misses even a few days of distribution (e.g., due to an upgrade that resets timers), `new_rounds_count` can reach thousands.
2. **Large neuron count**: An SNS with many neurons produces large ballot maps per proposal. The O(proposals × neurons) loop in Loop 2 can independently exhaust the instruction limit.
3. **No privileged access required**: `run_periodic_tasks` is invoked by the canister's own timer/heartbeat — no external actor needs to do anything. The vulnerability is triggered automatically by normal system operation.

### Recommendation

Apply the same batched, instruction-aware distribution pattern already used in NNS governance:

1. Cap `new_rounds_count` to a maximum per invocation (e.g., 100 rounds), rolling over the remainder to the next call.
2. Move the neuron maturity update loops into a `RewardsDistributionStateMachine`-style state machine that checks `is_message_over_threshold` after each neuron and resumes in subsequent timer ticks.
3. Add an explicit upper bound on the number of proposals processed per invocation.

### Proof of Concept

1. Deploy an SNS with `round_duration_seconds = 86400` (1 day) and a large number of neurons (e.g., 50,000).
2. Halt the SNS governance canister's periodic task for 30 days (e.g., by upgrading to a version that skips `run_periodic_tasks`, then upgrading back).
3. On the next `run_periodic_tasks` invocation, `new_rounds_count = 30`. Loop 2 then processes up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` proposals, each with 50,000 ballot entries — O(30 × proposals × 50,000) operations in a single message.
4. The canister traps on instruction limit. `latest_reward_event` is not updated. Every subsequent periodic invocation traps identically. No neuron in the SNS can claim maturity rewards. [7](#0-6) [1](#0-0)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5509-5521)
```rust
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }
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

**File:** rs/nns/governance/CHANGELOG.md (L655-670)
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
    * This will allow rewards to be compatible with neurons being stored in Stable Memory.
```
