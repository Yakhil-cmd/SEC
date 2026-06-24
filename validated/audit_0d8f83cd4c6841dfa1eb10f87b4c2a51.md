Audit Report

## Title
Unbounded Loops in SNS Governance `distribute_rewards` Can Permanently Block Voting Reward Distribution - (File: rs/sns/governance/src/governance.rs)

## Summary
The SNS governance canister's `distribute_rewards` function contains three unbounded loops with no instruction-limit guards. If any loop exhausts the IC instruction limit, the canister traps, all state changes are rolled back (including the write to `latest_reward_event`), and every subsequent periodic invocation faces the same or larger workload — creating a permanent, self-reinforcing denial of service for voting reward distribution across all SNS neurons.

## Finding Description
`distribute_rewards` is called synchronously from `run_periodic_tasks` at [1](#0-0)  with no batching or instruction-limit awareness.

**Loop 1** computes `new_rounds_count` without any cap at [2](#0-1)  and iterates over it unconditionally at [3](#0-2) . If the canister misses reward distribution (e.g., due to an upgrade), this value grows arbitrarily.

**Loop 2** iterates over all `considered_proposals` and all their ballots at [4](#0-3) , performing O(proposals × neurons) work with no instruction-limit break.

**Loop 3** iterates over every rewarded neuron to update maturity at [5](#0-4) , again with no instruction-limit check.

The critical invariant is that `self.proto.latest_reward_event` is only written at the very end of the function at [6](#0-5) . A trap at any point before this line rolls back all state, leaving `latest_reward_event` unchanged. The next invocation of `run_periodic_tasks` recomputes the same (or larger) `new_rounds_count`, triggering the same trap again.

A grep search for `is_message_over_threshold`, `is_over_instructions_limit`, and `instruction.*limit` in `rs/sns/governance/` returns zero matches, confirming no instruction-limit guard exists anywhere in the SNS governance reward path.

By contrast, NNS governance explicitly addresses this with a `RewardsDistributionStateMachine` that calls `is_over_instructions_limit` after each neuron at [7](#0-6) , and the NNS CHANGELOG documents this as a deliberate fix at [8](#0-7) .

## Impact Explanation
If the instruction limit is exhausted, no neuron in the SNS can ever receive maturity rewards again without a canister upgrade. This is a permanent, self-reinforcing application-level DoS on a core governance function (voting reward distribution) for all participants of the affected SNS. This matches the allowed High impact: **"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS"** and **"Significant SNS security impact with concrete user or protocol harm."**

## Likelihood Explanation
No privileged access is required. The vulnerability is triggered automatically by the canister's own timer/heartbeat via `run_periodic_tasks`. Two independent trigger paths exist:
1. **Missed rounds**: Any SNS governance canister upgrade that resets or delays timers causes `new_rounds_count` to accumulate. With a short `round_duration_seconds`, even a brief outage produces thousands of missed rounds.
2. **Large neuron count**: An SNS with many neurons produces large ballot maps per proposal. The O(proposals × neurons) loop in Loop 2 can independently exhaust the instruction limit under normal operation with no missed rounds at all.

Both conditions are realistic in production SNS deployments and require no attacker action.

## Recommendation
Apply the same batched, instruction-aware distribution pattern already used in NNS governance:
1. Cap `new_rounds_count` to a maximum per invocation (e.g., 100 rounds), rolling the remainder to the next call.
2. Introduce a `RewardsDistributionStateMachine`-style state machine for the neuron maturity update loop (Loop 3) that checks an instruction-limit predicate after each neuron and resumes in subsequent timer ticks.
3. Add an explicit upper bound on the number of proposals processed per invocation in Loop 2.

## Proof of Concept
1. Deploy an SNS with `round_duration_seconds = 86400` (1 day) and a large number of neurons (e.g., 50,000).
2. Upgrade the SNS governance canister to a version that skips `run_periodic_tasks`, wait 30+ days, then upgrade back to the normal version.
3. On the next `run_periodic_tasks` invocation, `new_rounds_count = 30`. Loop 2 then processes up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` proposals, each with 50,000 ballot entries — O(30 × proposals × 50,000) operations in a single message.
4. The canister traps on instruction limit. `latest_reward_event` is not updated (write is at line 6084, after all loops). Every subsequent periodic invocation traps identically.
5. Verify: query `latest_reward_event` before and after multiple `run_periodic_tasks` invocations — it never advances, and no neuron's `maturity_e8s_equivalent` increases.

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

**File:** rs/sns/governance/src/governance.rs (L6084-6092)
```rust
        self.proto.latest_reward_event = Some(RewardEvent {
            round: new_reward_event_round,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent,
            end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
            rounds_since_last_distribution: Some(new_rounds_count),
            total_available_e8s_equivalent,
        })
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
