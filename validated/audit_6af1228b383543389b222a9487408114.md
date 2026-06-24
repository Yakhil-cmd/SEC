Audit Report

## Title
Unbounded Iteration in SNS `distribute_rewards` Can Permanently Halt Voting Reward Distribution - (File: `rs/sns/governance/src/governance.rs`)

## Summary
The SNS governance canister's `distribute_rewards` function executes two unbounded loops — one over all ballots of all `ReadyToSettle` proposals and one over all rewarded neurons — within a single message execution, with no instruction-limit guard. If the combined work exceeds the IC per-message instruction limit, the entire function traps and rolls back, leaving `latest_reward_event` unadvanced. Because `should_distribute_rewards()` then returns `true` again, every subsequent timer tick re-enters the same path and traps identically, permanently preventing voting rewards from being distributed.

## Finding Description
`run_periodic_tasks` calls `distribute_rewards` when `should_distribute_rewards()` returns `true`: [1](#0-0) 

Inside `distribute_rewards`, **Loop 1** iterates over every ballot of every `ReadyToSettle` proposal to build `neuron_id_to_reward_shares`. With the ceiling values of 700 proposals and 200,000 neurons, this is up to 140 million iterations: [2](#0-1) 

**Loop 2** then iterates over every entry in `neuron_id_to_reward_shares`, performing `Decimal` division per neuron: [3](#0-2) 

Neither loop contains any call to `is_message_over_threshold` or any other instruction-budget check. A `grep` for `is_message_over_threshold` in `rs/sns/**` returns zero matches, confirming no guard exists anywhere in this path.

`latest_reward_event` is only written at the very end of the function: [4](#0-3) 

If the message traps before reaching this line, all state changes roll back, `latest_reward_event` remains unadvanced, and `should_distribute_rewards()` returns `true` on the next timer tick, causing the identical trap to repeat indefinitely.

The ceiling constants that bound the worst-case iteration count are: [5](#0-4) 

**Contrast with NNS governance**, which already fixed this exact pattern. NNS `distribute_pending_rewards` uses `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` and a `RewardsDistributionStateMachine` backed by `StableBTreeMap` to persist and resume work across timer ticks: [6](#0-5) [7](#0-6) 

## Impact Explanation
This is a permanent, self-reinforcing DoS of voting reward distribution for any affected SNS. Once the instruction limit is hit, no neuron ever receives maturity for the affected reward period, and no future reward period can begin because `latest_reward_event` is never advanced. This constitutes a significant SNS governance security impact with concrete, irreversible user harm — matching the **High** bounty impact class: *"Significant SNS or infrastructure security impact with concrete user or protocol harm."*

## Likelihood Explanation
No adversarial action is required. Any unprivileged principal can stake SNS tokens to create neurons; any neuron holder above the minimum stake threshold can submit proposals. As a popular SNS grows organically toward the default ceilings (`max_number_of_neurons = 200,000`, `max_number_of_proposals_with_ballots = 700`), the condition is reached through normal operation. The NNS team already recognized this as a real production risk and shipped the chunked-distribution fix for NNS governance, confirming the threat model is realistic. [8](#0-7) 

## Recommendation
Port the NNS chunked-distribution pattern to SNS governance:
1. Introduce a `RewardsDistributionStateMachine` (or equivalent) backed by stable memory for SNS.
2. In `distribute_rewards`, compute `neuron_id_to_reward_shares`, persist it to the state machine, and return without mutating neuron maturity or `latest_reward_event`.
3. In a separate periodic task (or the same timer), call `continue_processing` with an `is_message_over_threshold` guard that breaks the loop and re-schedules when the instruction budget is nearly exhausted.
4. Only update `latest_reward_event` and settle proposals after the distribution is fully complete.

## Proof of Concept
1. Deploy an SNS with `max_number_of_neurons = 200_000` and `max_number_of_proposals_with_ballots = 700`.
2. Create 200,000 neurons (each staking the minimum SNS token amount).
3. Submit 700 proposals and have all neurons vote on each.
4. Wait for the reward round to end; `should_distribute_rewards()` returns `true`.
5. The timer fires `run_periodic_tasks` → `distribute_rewards`.
6. Loop 1 iterates ~140 million ballot entries; Loop 2 iterates ~200,000 neurons with `Decimal` arithmetic.
7. The message traps due to instruction limit exhaustion; all state rolls back.
8. Every subsequent timer tick repeats steps 5–7 indefinitely.
9. No neuron ever receives voting rewards; `latest_reward_event` is never advanced.

A deterministic integration test using PocketIC can reproduce this by populating the SNS state to the ceiling values and asserting that `latest_reward_event.round` never advances across multiple timer ticks.

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L5893-5931)
```rust
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

**File:** rs/sns/governance/src/types.rs (L386-390)
```rust
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

**File:** rs/sns/governance/api_helpers/src/lib.rs (L32-37)
```rust
        max_number_of_neurons: Some(200_000),
        neuron_minimum_dissolve_delay_to_vote_seconds: Some(6 * ONE_MONTH_SECONDS), // 6m
        max_followees_per_function: Some(15),
        max_dissolve_delay_seconds: Some(8 * ONE_YEAR_SECONDS), // 8y
        max_neuron_age_for_age_bonus: Some(4 * ONE_YEAR_SECONDS), // 4y
        max_number_of_proposals_with_ballots: Some(700),
```
