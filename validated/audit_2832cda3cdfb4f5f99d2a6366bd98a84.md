Audit Report

## Title
Unbounded Loop in SNS Governance `distribute_rewards` Causes Permanent Instruction-Limit DoS - (File: rs/sns/governance/src/governance.rs)

## Summary

The SNS Governance canister's `distribute_rewards` function performs two unbounded nested loops over all `ReadyToSettle` proposals and their ballot maps in a single synchronous execution with no instruction-limit guard. An unprivileged token holder can grow these collections by staking neurons and voting on proposals until `distribute_rewards` permanently traps on every heartbeat invocation, freezing voting-reward distribution for the entire SNS without any path to recovery short of a canister upgrade.

## Finding Description

`distribute_rewards` is a synchronous function called from `run_periodic_tasks` (the heartbeat handler) at [1](#0-0)  whenever `should_distribute_rewards()` returns `true`.

Inside `distribute_rewards`, **Loop 1** iterates over every proposal in `considered_proposals` (all `ReadyToSettle` proposals, collected with no cap at [2](#0-1) ) and for each proposal iterates over every ballot entry: [3](#0-2) 

**Loop 2** then iterates over every neuron that cast a vote to credit maturity: [4](#0-3) 

A grep for `is_message_over_threshold` across all SNS governance source returns zero matches, confirming neither loop contains any instruction-limit guard. Total work is O(P × N) where P = number of `ReadyToSettle` proposals and N = number of voting neurons.

By contrast, the NNS Governance canister already fixed this identical pattern: its `RewardsDistributionInProgress::continue_processing` checks `is_over_instructions_limit()` after each neuron and breaks across multiple timer messages: [5](#0-4) 

The SNS `distribute_rewards` has received no equivalent treatment.

**Attacker-controlled growth:** Any user can stake SNS tokens to create neurons (up to `max_number_of_neurons`) and vote on every open proposal, adding one ballot entry per neuron per proposal. Proposals accumulate in `ReadyToSettle` state until the next reward event; `maybe_gc` only purges proposals that `can_be_purged`, which excludes unsettled proposals. Once the instruction limit is exceeded, the canister traps, state is rolled back, and the condition persists on every subsequent heartbeat.

## Impact Explanation

This is a **High** severity application/platform-level DoS. Once triggered, voting rewards are permanently frozen: `distribute_rewards` traps on every heartbeat, no maturity is ever credited to neuron holders, and the economic incentive to participate in SNS governance is eliminated. Recovery requires a canister upgrade, which itself requires governance participation — creating a potential deadlock. This matches the allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact."*

## Likelihood Explanation

No privileged access is required. Any token holder can stake neurons and vote. The growth is organic — a popular SNS with active governance will naturally accumulate enough proposals and ballots to trigger this without a deliberate attacker. The NNS team already identified and fixed the identical pattern in NNS governance, confirming realistic likelihood. The attack is repeatable and permanent once triggered.

## Recommendation

Apply the same batched-distribution pattern already used in NNS governance (`rs/nns/governance/src/reward/distribution.rs`):

1. Introduce a persistent `RewardsDistributionInProgress` state in SNS governance that stores per-neuron reward shares after the calculation phase.
2. Process maturity credits in a separate periodic timer task that calls `is_message_over_threshold` after each neuron and suspends across messages until the map is drained.
3. Alternatively, add an explicit cap on the number of proposals processed per `distribute_rewards` invocation and carry a cursor across calls, analogous to `prune_some_following` in NNS governance.

## Proof of Concept

1. Deploy an SNS with `max_number_of_neurons` set to a large value (e.g., 10,000).
2. Have 10,000 users each stake tokens and create a neuron.
3. Submit 50 proposals and have all 10,000 neurons vote on each (500,000 ballot entries total).
4. Allow the proposals' voting periods to expire so they enter `ReadyToSettle`.
5. Wait for the next reward period to elapse so `should_distribute_rewards()` returns `true`.
6. Observe that `run_periodic_tasks` → `distribute_rewards` traps with `CanisterInstructionLimitExceeded` on every heartbeat invocation.
7. Confirm that voting rewards are never distributed again without a canister upgrade by inspecting the `latest_reward_event` — it never advances.

A deterministic integration test using PocketIC can reproduce this by mocking `env.now()` to advance past the reward period and populating `proto.proposals` with the required number of `ReadyToSettle` proposals each containing `max_number_of_neurons` ballot entries, then asserting that `distribute_rewards` panics or that the instruction counter exceeds the subnet limit.

### Citations

**File:** rs/sns/governance/src/governance.rs (L5509-5513)
```rust
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
```

**File:** rs/sns/governance/src/governance.rs (L5822-5823)
```rust
        let considered_proposals: Vec<ProposalId> =
            self.ready_to_be_settled_proposal_ids().collect();
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
