### Title
Unbounded Iteration in SNS Governance `distribute_rewards` Causes Instruction-Limit Exhaustion and Permanent Reward Distribution Failure - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister's `distribute_rewards` function iterates over all settled proposals and all their ballots (one per eligible neuron) in a single synchronous message with no instruction-limit guard. As an SNS grows — neurons created by unprivileged stakers, proposals created by token holders — this O(P × N) loop will eventually exceed the Wasm instruction limit, causing the heartbeat to trap and permanently halting maturity distribution to all neuron holders. The NNS governance already fixed the identical pattern (proposal 135702), but the SNS governance retains the unbounded design.

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` is called synchronously from `run_periodic_tasks`: [1](#0-0) 

Inside `distribute_rewards`, two unbounded loops execute without any instruction-limit check:

**Loop 1 — O(P × N) ballot scan**: For every proposal in `considered_proposals`, every ballot (one per eligible neuron at proposal creation time) is read to accumulate `neuron_id_to_reward_shares`. [2](#0-1) 

**Loop 2 — O(N) neuron mutation**: Every entry in `neuron_id_to_reward_shares` is applied to the neuron store with no break condition. [3](#0-2) 

Additionally, the `new_rounds_count` loop that computes the reward purse is itself O(missed rounds), compounding the cost when the heartbeat has been failing: [4](#0-3) 

The NNS governance identified and fixed this exact pattern. Its CHANGELOG for proposal 135702 states: *"Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages."* The NNS now uses `RewardsDistributionStateMachine` with `is_message_over_threshold` guards: [5](#0-4) 

The SNS governance has no equivalent mechanism. Its `distribute_rewards` is a single atomic synchronous call with no escape hatch. [6](#0-5) 

### Impact Explanation

When the instruction limit is exceeded, the heartbeat message traps. Because `distribute_rewards` is called inside `run_periodic_tasks` — which also handles upgrade checks, maturity modulation, and GC — a trap in `distribute_rewards` prevents all periodic work from completing. More critically:

- Voting rewards are never credited to neuron `maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent`.
- The `latest_reward_event` is not updated, so the next heartbeat recalculates from the same stale baseline, making `new_rounds_count` grow, which makes the purse-computation loop longer, which makes the problem self-reinforcing.
- Neuron holders permanently lose accrued maturity — the economic analog of the Splitter's unclaimable fees.

### Likelihood Explanation

The entry path is fully unprivileged:

1. Any user can stake SNS tokens to create neurons, increasing N.
2. Any neuron holder with sufficient voting power can submit proposals, increasing P.
3. `compute_ballots_for_new_proposal` iterates over all eligible neurons to populate each proposal's ballot map, so each new proposal adds N ballot entries to future `distribute_rewards` calls. [7](#0-6) 

With 10 000 neurons and 100 settled proposals, Loop 1 processes 1 000 000 ballot entries. Each entry involves a `HashMap` lookup and floating-point arithmetic. At realistic per-iteration costs this approaches or exceeds the 40-billion-instruction per-message limit. A moderately successful SNS with active governance reaches this scale organically.

### Recommendation

Apply the same fix as NNS governance:

1. Move reward distribution out of `run_periodic_tasks` into a dedicated timer task.
2. Introduce a `RewardsDistributionInProgress` state machine that checkpoints progress across messages.
3. Add an `is_message_over_threshold` guard inside the neuron-mutation loop so each timer invocation processes a bounded batch and reschedules itself if work remains. [8](#0-7) [9](#0-8) 

### Proof of Concept

1. Deploy an SNS with `voting_rewards_parameters` set and a short `round_duration_seconds`.
2. Have 10 000 distinct principals each stake tokens and claim a neuron (all unprivileged operations).
3. Submit and settle 100 governance proposals over time (each proposal's ballot map contains 10 000 entries).
4. Observe that `run_periodic_tasks` → `distribute_rewards` now must scan 1 000 000 ballot entries in Loop 1 plus mutate 10 000 neuron records in Loop 2 within a single heartbeat message.
5. The heartbeat traps on instruction-limit exhaustion; `latest_reward_event` is never updated; no neuron receives maturity; subsequent heartbeats repeat the same over-limit computation because `new_rounds_count` keeps growing, making recovery impossible without an upgrade. [10](#0-9)

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

**File:** rs/sns/governance/src/governance.rs (L5503-5514)
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
```

**File:** rs/sns/governance/src/governance.rs (L5763-5764)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
```

**File:** rs/sns/governance/src/governance.rs (L5812-5820)
```rust
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
