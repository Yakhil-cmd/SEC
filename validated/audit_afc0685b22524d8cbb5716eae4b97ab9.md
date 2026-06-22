### Title
Unbounded Loop Over All Neuron Ballots in SNS Governance Reward Distribution Causes Permanent DoS - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister's `distribute_rewards` function contains unbounded nested loops over all settled proposals and all neuron ballots with no instruction-limit check or batching mechanism. As the number of SNS neurons grows, this function will exceed the IC per-message instruction limit, permanently halting voting reward distribution for the SNS.

### Finding Description

`distribute_rewards` in the SNS governance canister executes two unbounded loops in a single synchronous message with no instruction-limit guard:

**Loop 1** — iterates over every `considered_proposal` and, for each, over every `ballot` (one per eligible neuron at proposal creation time): [1](#0-0) 

**Loop 2** — iterates over every entry in `neuron_id_to_reward_shares` (one per voting neuron) to credit maturity: [2](#0-1) 

Neither loop contains any call to an instruction counter, any `break` on resource exhaustion, or any batching/resumption mechanism. The entire function runs atomically in one message execution.

This function is called from `run_periodic_tasks` every reward round: [3](#0-2) 

**Contrast with NNS governance**, which was explicitly refactored to fix this exact class of bug. NNS now uses `RewardsDistributionStateMachine::continue_processing`, which checks `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` after each neuron and breaks, resuming in the next timer tick: [4](#0-3) 

The NNS CHANGELOG explicitly documents this fix: [5](#0-4) 

SNS governance has no equivalent protection.

Additionally, `compute_ballots_for_new_proposal` in SNS governance iterates over all neurons in `self.proto.neurons` with no instruction limit, making proposal submission also vulnerable at scale: [6](#0-5) 

### Impact Explanation

When the number of SNS neurons is large enough that the nested ballot loops exceed the IC per-message instruction limit (~40 billion instructions), `distribute_rewards` traps on every invocation. Because SNS has no batched fallback path, voting rewards are permanently frozen — no neuron ever receives maturity for voting. This is a complete DoS of the SNS reward mechanism. Additionally, if `compute_ballots_for_new_proposal` also traps, new proposals cannot be submitted, halting SNS governance entirely.

### Likelihood Explanation

SNS `NervousSystemParameters` allows `max_number_of_neurons` to be set up to 200,000. Any SNS that accumulates tens of thousands of neurons — achievable organically through normal staking activity, or accelerated by an attacker who stakes minimal amounts to create many neurons — will trigger this condition. The attacker needs no privileged role: staking SNS tokens to create neurons is an unprivileged operation open to any token holder. The cost of the attack scales with the SNS token price but is bounded by the `max_number_of_neurons` cap.

### Recommendation

Apply the same fix used in NNS governance:

1. **Short term**: Add an instruction-limit check inside the ballot-iteration loop in `distribute_rewards`. If the limit is reached, persist the partially-computed `neuron_id_to_reward_shares` map and resume in the next periodic task invocation.
2. **Long term**: Refactor SNS `distribute_rewards` to use a state-machine pattern analogous to NNS `RewardsDistributionStateMachine`, separating the reward calculation phase from the maturity-crediting phase and processing neurons in bounded batches across multiple timer ticks.

### Proof of Concept

1. Deploy an SNS with `voting_rewards_parameters` enabled and `max_number_of_neurons` set to a large value (e.g., 50,000).
2. Have many principals stake SNS tokens to create neurons (each with the minimum dissolve delay to be eligible to vote).
3. Submit a proposal and let it reach `ReadyToSettle` status.
4. When the reward round elapses, `run_periodic_tasks` calls `distribute_rewards`. The function enters the nested loop over `considered_proposals × ballots` (50,000 entries) and then the loop over `neuron_id_to_reward_shares` (50,000 entries). The instruction counter is exhausted; the message traps.
5. On every subsequent reward round, the same trap recurs. No neuron ever receives maturity. The SNS reward mechanism is permanently halted.

### Citations

**File:** rs/sns/governance/src/governance.rs (L5255-5280)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }
```

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
