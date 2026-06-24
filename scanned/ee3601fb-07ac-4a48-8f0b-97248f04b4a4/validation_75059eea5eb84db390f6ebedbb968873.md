### Title
Unbounded Single-Message Reward Distribution Loop in SNS Governance Can Permanently DOS Periodic Tasks - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister's `distribute_rewards` function iterates over all ballots of all settled proposals and all rewarded neurons in a single synchronous message with no instruction-limit check or batching. With the maximum allowed neuron count (200,000) and proposal count (700), the ballot iteration alone can reach ~140 million entries, exceeding the IC per-message instruction limit (~40 billion instructions). Because the function traps before updating `latest_reward_event`, the state rolls back and every subsequent timer invocation re-attempts and re-traps, permanently blocking reward distribution for the SNS.

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` is called synchronously from `run_periodic_tasks` with no `await` points and no instruction-limit guard: [1](#0-0) 

The function body contains three unbounded loops executed in a single message:

**Loop 1 — ballot aggregation** (up to `max_number_of_proposals_with_ballots` × `max_number_of_neurons` iterations): [2](#0-1) 

**Loop 2 — neuron maturity update** (up to `max_number_of_neurons` iterations): [3](#0-2) 

**Loop 3 — proposal settlement** (up to `max_number_of_proposals_with_ballots` iterations): [4](#0-3) 

`latest_reward_event` is only written at the very end of the function: [5](#0-4) 

If the function traps before reaching that line, the IC rolls back all state changes. The next timer invocation calls `should_distribute_rewards()`, which checks `latest_reward_event` — still showing the old round — and calls `distribute_rewards` again, trapping again. The cycle repeats indefinitely.

The SNS parameter ceilings that bound the work are: [6](#0-5) 

With `MAX_NUMBER_OF_NEURONS_CEILING = 200,000` and `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING = 700`, Loop 1 can process up to 140 million ballot entries in one message.

**Contrast with NNS governance (already fixed):** The NNS governance canister explicitly batches reward distribution across multiple timer messages using `is_message_over_threshold`: [7](#0-6) 

The NNS CHANGELOG documents this exact class of fix: [8](#0-7) 

The SNS governance has not received the equivalent fix.

### Impact Explanation

Reward distribution is permanently halted for the affected SNS. Neurons that voted on proposals never receive maturity rewards. The `run_periodic_tasks` timer continues to fire but always traps at `distribute_rewards`, consuming subnet resources and blocking all work that follows it in the same timer callback (maturity finalization, metrics, GC). The SNS governance canister cannot self-recover without an upgrade that either batches the work or resets `latest_reward_event` manually.

### Likelihood Explanation

An unprivileged SNS participant can:
1. Stake tokens and create neurons up to `max_number_of_neurons` (default 200,000, ceiling 200,000).
2. Submit proposals up to `max_number_of_proposals_with_ballots` (default 700, ceiling 700) and have all neurons vote on each.
3. Wait for the reward round to end.

No privileged access, no governance majority, and no threshold attack is required. The cost is proportional to the SNS token's `reject_cost_e8s` and `neuron_minimum_stake_e8s`, which are SNS-configurable but typically low. For SNS instances with many active participants and high proposal throughput, this condition can arise organically without any adversarial intent.

### Recommendation

Apply the same batching pattern already used in NNS governance:

1. After calculating `neuron_id_to_reward_shares` and updating `latest_reward_event`, persist the pending distribution to stable storage (analogous to `RewardsDistributionStateMachine`).
2. Process neuron maturity updates in a separate periodic timer task that checks `is_message_over_threshold` after each neuron and resumes in the next message if the limit is reached.
3. Add an instruction-limit guard to the ballot-aggregation loop (Loop 1), or cap `considered_proposals` to a safe batch size per invocation.

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons = 200_000` and `max_number_of_proposals_with_ballots = 700`.
2. Create 200,000 neurons (each staking the minimum).
3. Submit 700 proposals and have all 200,000 neurons vote on each.
4. Wait for the reward round to end (`should_distribute_rewards()` returns `true`).
5. Observe that the next `run_periodic_tasks` timer invocation traps (instruction limit exceeded in `distribute_rewards`).
6. Observe that `latest_reward_event` is unchanged (state rolled back).
7. Observe that every subsequent timer invocation also traps — reward distribution is permanently halted.

The root cause is at: [9](#0-8) 

with the unbounded ballot loop at: [10](#0-9) 

and the unbounded neuron loop at: [11](#0-10)

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

**File:** rs/sns/governance/src/governance.rs (L5763-5765)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
        let now = self.env.now();
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

**File:** rs/sns/governance/src/governance.rs (L6013-6081)
```rust
        for pid in &considered_proposals {
            // Before considering a proposal for reward, it must be fully processed --
            // because we're about to clear the ballots, so no further processing will be
            // possible.
            self.process_proposal(pid.id);

            let p = match self.get_proposal_data_mut(*pid) {
                Some(p) => p,
                None => {
                    log!(
                        ERROR,
                        "Cannot find proposal {}, despite it being considered for rewards distribution.",
                        pid.id
                    );
                    debug_assert!(
                        false,
                        "It appears that proposal {} has been deleted out from under us \
                         while we were distributing rewards. This should never happen. \
                         In production, this would be quietly swept under the rug and \
                         we would continue processing. Current state (Governance):\n{:#?}",
                        pid.id, self.proto,
                    );
                    continue;
                }
            };

            if p.status() == ProposalDecisionStatus::Open {
                log!(
                    ERROR,
                    "Proposal {} was considered for reward distribution despite \
                     being open. We will now force the proposal's status to be Rejected.",
                    pid.id
                );
                debug_assert!(
                    false,
                    "This should be unreachable. Current governance state:\n{:#?}",
                    self.proto,
                );

                // The next two statements put p into the Rejected status. Thus,
                // process_proposal will consider that it has nothing more to do
                // with the p.
                p.decided_timestamp_seconds = now;
                p.latest_tally = Some(Tally {
                    timestamp_seconds: now,
                    yes: 0,
                    no: 0,
                    total: 0,
                });
                debug_assert_eq!(
                    p.status(),
                    ProposalDecisionStatus::Rejected,
                    "Failed to force ProposalData status to become Rejected. p:\n{p:#?}",
                );
            }

            // This is where the proposal becomes Settled, at least in the eyes
            // of the ProposalData::reward_status method.
            p.reward_event_end_timestamp_seconds = Some(reward_event_end_timestamp_seconds);
            p.reward_event_round = new_reward_event_round;

            // Ballots are used to determine two things:
            //   1. (obviously and primarily) whether to execute the proposal.
            //   2. rewards
            // At this point, we no longer need ballots for either of these
            // things, and since they take up a fair amount of space, we take
            // this opportunity to jettison them.
            p.ballots.clear();
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

**File:** rs/nns/governance/CHANGELOG.md (L654-669)
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
```
