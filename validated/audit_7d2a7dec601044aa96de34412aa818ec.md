### Title
Unbounded Loop in `distribute_rewards` Can Exhaust Instruction Limit, Permanently Blocking SNS Reward Distribution - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister's `distribute_rewards` function iterates over all ready-to-settle proposals, all ballots per proposal, and all rewarded neurons in a single synchronous call with no instruction-limit guard. If the accumulated set of proposals and voting neurons is large enough, the function will trap on the instruction limit every time it is invoked, permanently preventing voting rewards from being distributed.

### Finding Description
`distribute_rewards` in `rs/sns/governance/src/governance.rs` is called from `run_periodic_tasks` (the canister heartbeat) whenever `should_distribute_rewards()` returns true. The function contains three nested/sequential unbounded loops:

1. **Outer loop over all ready-to-settle proposals** (line 5894): `for proposal_id in &considered_proposals`
2. **Inner loop over all ballots per proposal** (line 5896): `for (voter, ballot) in &proposal.ballots`
3. **Final loop over all rewarded neurons** (line 5954): `for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares`

None of these loops contain any instruction-limit check. The entire computation — summing reward shares across every ballot of every proposal, then writing maturity to every rewarded neuron — must complete within a single message execution.

This is in direct contrast to the NNS governance canister, which was explicitly fixed to batch reward distribution across multiple timer-driven messages using `is_message_over_threshold` and `continue_processing` with a per-iteration limit check. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
When the instruction limit is exceeded, the IC rolls back the entire heartbeat execution. The `latest_reward_event` is never updated, so `should_distribute_rewards()` returns `true` again on the next heartbeat, which immediately re-enters `distribute_rewards` and traps again. This creates a permanent livelock: voting rewards are never distributed, neuron maturity never increases, and the SNS governance canister's reward mechanism is effectively dead for as long as the proposal/ballot set remains large. Neurons cannot receive staking rewards, breaking a core economic incentive of the SNS. [4](#0-3) 

### Likelihood Explanation
An SNS with active governance accumulates proposals over time. Each proposal stores one ballot per neuron that voted. A moderately active SNS with 1,000 neurons and 50 proposals settling in the same reward round produces 50,000 ballot entries to iterate, plus 1,000 neuron maturity writes — well within the range that can exhaust the per-message instruction limit (currently ~20 billion instructions on application subnets, but each stable-memory neuron write is expensive). Any SNS token holder can submit proposals and vote, making this reachable without any privileged access. [5](#0-4) 

### Recommendation
Apply the same batching pattern already used in NNS governance:

1. In `distribute_rewards`, compute the reward shares and store the per-neuron amounts in a persistent queue (analogous to `RewardsDistributionStateMachine`).
2. Introduce a separate periodic timer task that drains the queue in bounded batches, checking `is_message_over_threshold` after each neuron write and breaking when the limit is approached.
3. Only advance `latest_reward_event` after the queue is fully drained, or use a two-phase commit (mark event as "pending distribution" first, then "settled" after the queue empties). [6](#0-5) 

### Proof of Concept

**Attacker-controlled entry path:**

1. Deploy or interact with an SNS that has a large number of neurons (e.g., 2,000+).
2. Submit many proposals (e.g., 30+) and have neurons vote on all of them. Each proposal accumulates one ballot per voting neuron.
3. Allow the proposals to pass their voting period so they enter `ReadyToSettle` state.
4. When the reward round ends, `run_periodic_tasks` calls `should_distribute_rewards()` → `true`, then calls `distribute_rewards(supply)`.
5. `distribute_rewards` enters the nested loops: 30 proposals × 2,000 ballots = 60,000 ballot iterations, followed by 2,000 neuron maturity writes (each touching stable memory).
6. The message traps on the instruction limit. The state rolls back. `latest_reward_event` is unchanged.
7. On the next heartbeat, step 4–6 repeats indefinitely. No rewards are ever distributed.

The root cause is at: [7](#0-6) 

compared to the NNS fix: [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5503-5521)
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
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }
```

**File:** rs/sns/governance/src/governance.rs (L5763-5764)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
```

**File:** rs/sns/governance/src/governance.rs (L5861-5875)
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

            result
        };
```

**File:** rs/sns/governance/src/governance.rs (L5894-5931)
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

**File:** rs/nns/governance/src/timer_tasks/distribute_rewards.rs (L43-55)
```rust
impl PeriodicSyncTask for DistributeRewardsTask {
    fn execute(self) {
        self.governance.with_borrow_mut(|governance| {
            let work_left = governance.distribute_pending_rewards();
            if !work_left {
                cancel_distribute_pending_rewards_timer();
            }
        });
    }

    const NAME: &'static str = "distribute_rewards";
    const INTERVAL: Duration = Duration::from_secs(2);
}
```
