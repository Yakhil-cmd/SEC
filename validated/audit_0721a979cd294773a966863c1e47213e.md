### Title
Unbounded Neuron Ballot Iteration in SNS Governance `distribute_rewards` Exhausts Instruction Limit, Permanently Halting Reward Distribution - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance `distribute_rewards` function iterates over all ballots of all `ReadyToSettle` proposals and all voting neurons in a single synchronous execution with no instruction-limit guard. With `max_number_of_neurons` at `MAX_NUMBER_OF_NEURONS_CEILING = 200,000` and `max_number_of_proposals_with_ballots` at `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING = 700`, the combined ballot iteration can reach ~140 million entries, far exceeding the IC's 5-billion-instruction per-message limit. When the timer callback traps, state is rolled back and the next invocation reproduces the same failure, permanently halting reward distribution for the SNS.

---

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` contains two unbounded loops with no instruction-limit check:

**Loop 1 — ballot aggregation (lines 5894–5930):**
```rust
for proposal_id in &considered_proposals {
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots {
            // NeuronId::from_str + HashMap entry + arithmetic
        }
    }
}
```
`considered_proposals` can hold up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING = 700` entries; each proposal's ballot map can hold up to `MAX_NUMBER_OF_NEURONS_CEILING = 200,000` entries. Worst-case: 700 × 200,000 = **140 million iterations**, each performing string parsing (`NeuronId::from_str`), a `HashMap` entry operation, and `Decimal` arithmetic — conservatively ~1,000–5,000 instructions each → **140 billion – 700 billion instructions**, far above the 5B limit. [1](#0-0) 

**Loop 2 — maturity distribution (lines 5954–5997):**
```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) { ... };
    // Decimal division + neuron mutation
}
```
Up to 200,000 neurons, each requiring a mutable neuron lookup and `Decimal` arithmetic, with no instruction-limit break. [2](#0-1) 

**Additional amplifier — `new_rounds_count` loop (lines 5861–5872):**
For a new SNS where `latest_reward_event().end_timestamp_seconds` is `None`, `unwrap_or_default()` returns `0`, so `new_rounds_count = now / round_duration_seconds`. With the minimum `round_duration_seconds = 86400` (1 day) and a typical Unix timestamp of ~1.7 × 10⁹, this yields ~19,675 iterations of arithmetic on the first invocation. [3](#0-2) 

`distribute_rewards` is called synchronously from `run_periodic_tasks`, which is scheduled as a repeating timer: [4](#0-3) [5](#0-4) 

**The NNS governance already fixed this exact class of bug** (Proposal 135702), explicitly noting: *"Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages"* and using `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` to break the loop: [6](#0-5) [7](#0-6) [8](#0-7) 

The SNS governance has received no equivalent fix. Its `distribute_rewards` remains a single atomic, unbounded execution. [9](#0-8) 

The SNS neuron ceiling is defined as: [10](#0-9) 

---

### Impact Explanation

When the instruction limit is exceeded inside a timer callback on the IC, the execution traps and all state mutations are rolled back. Because the condition that caused the overflow (large neuron count + large ballot count) is persistent state, every subsequent timer invocation reproduces the same trap. The SNS governance canister enters a permanent liveness failure: voting rewards are never distributed, neuron maturity never increases, and the `latest_reward_event` is never updated. This effectively freezes the economic incentive layer of the SNS, degrading governance participation and token holder trust.

---

### Likelihood Explanation

Any SNS that configures `max_number_of_neurons` close to `MAX_NUMBER_OF_NEURONS_CEILING = 200,000` and `max_number_of_proposals_with_ballots` close to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING = 700` is at risk. These are governance-controlled parameters; an SNS community may raise them to support growth. An unprivileged participant who creates neurons and votes on proposals (normal, incentivized behavior) organically drives the system toward the failure threshold. No special privilege or coordination is required beyond normal participation at scale.

---

### Recommendation

Apply the same pattern already used in NNS governance:

1. Move the per-neuron maturity increment out of `distribute_rewards` and into a separate batched timer task (analogous to `DistributeRewardsTask` + `RewardsDistributionStateMachine` in NNS).
2. Store the pending per-neuron reward map in stable memory after `distribute_rewards` computes it.
3. Process the map in chunks across multiple timer invocations, checking `is_message_over_threshold` after each neuron update and breaking when the limit is approached.
4. Cap `new_rounds_count` to a safe maximum (e.g., 1,000) per invocation, or initialize `reward_start_timestamp_seconds` from `genesis_timestamp_seconds` rather than defaulting to Unix epoch 0.

---

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons = 200_000` and `max_number_of_proposals_with_ballots = 700`.
2. Create 200,000 neurons (achievable by any token holder staking the minimum stake).
3. Submit 700 proposals and have all neurons vote on each (via following, this requires only the root neuron to vote).
4. Wait for `run_periodic_tasks` to fire (every `RUN_PERIODIC_TASKS_INTERVAL`).
5. `should_distribute_rewards` returns `true`; `distribute_rewards` is called.
6. The ballot aggregation loop at line 5894 iterates 700 × 200,000 = 140 million times, each calling `NeuronId::from_str` and a `HashMap` entry operation.
7. The IC instruction counter exceeds 5 billion; the timer callback traps; state is rolled back.
8. `latest_reward_event` is never updated; `should_distribute_rewards` returns `true` again on the next tick.
9. The SNS governance canister is permanently unable to distribute voting rewards.

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

**File:** rs/sns/governance/src/governance.rs (L5763-5765)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
        let now = self.env.now();
```

**File:** rs/sns/governance/src/governance.rs (L5808-5872)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
        if new_rounds_count == 0 {
            // This may happen, in case consider_distributing_rewards was called
            // several times at almost the same time. This is
            // harmless, just abandon.
            return;
        }

        let considered_proposals: Vec<ProposalId> =
            self.ready_to_be_settled_proposal_ids().collect();
        // RewardEvents are generated every time. If there are no proposals to reward, the rewards
        // purse is rolled over via the total_available_e8s_equivalent field.

        // Log if we are about to "backfill" rounds that were missed.
        if new_rounds_count > 1 {
            log!(
                INFO,
                "Some reward distribution should have happened, but were missed. \
                 It is now {}. Whereas, latest_reward_event:\n{:#?}",
                now,
                self.latest_reward_event(),
            );
        }
        let reward_event_end_timestamp_seconds = new_rounds_count
            .saturating_mul(round_duration_seconds)
            .saturating_add(reward_start_timestamp_seconds);

        // What's going on here looks a little complex, but it's just a slightly
        // more advanced version of simple (i.e. non-compounding) interest. The
        // main embellishment is because we are calculating the reward purse
        // over possibly more than one reward round. The possibility of multiple
        // rounds is why we loop over rounds. Otherwise, it boils down to the
        // simple interest formula:
        //
        //   principal * rate * duration
        //
        // Here, the entire token supply is used as the "principal", and the
        // length of a reward round is used as the duration. The reward rate
        // varies from round to round, and is calculated using
        // VotingRewardsParameters::reward_rate_at.
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
```

**File:** rs/sns/governance/src/governance.rs (L5892-5930)
```rust
        // Add up reward shares based on voting power that was exercised.
        let mut neuron_id_to_reward_shares: HashMap<NeuronId, Decimal> = HashMap::new();
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

**File:** rs/sns/governance/canister/canister.rs (L632-634)
```rust
    let new_timer_id = ic_cdk_timers::set_timer_interval(RUN_PERIODIC_TASKS_INTERVAL, async || {
        run_periodic_tasks().await
    });
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

**File:** rs/nns/governance/CHANGELOG.md (L654-670)
```markdown
    * Compared to the last time it was enabled, several improvements were made:
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

**File:** rs/sns/governance/src/types.rs (L383-390)
```rust
    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;

    /// This is an upper bound for `max_number_of_proposals_with_ballots`. Exceeding
    /// it may cause degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING: u64 = 700;
```
