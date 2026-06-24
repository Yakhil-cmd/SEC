Audit Report

## Title
Unbounded Ballot Iteration in SNS Governance `distribute_rewards` Causes Permanent DoS on Voting Reward Distribution - (File: `rs/sns/governance/src/governance.rs`)

## Summary
The SNS Governance canister's `distribute_rewards` function performs two unbounded loops over all ballots of all `ReadyToSettle` proposals and all voting neurons in a single canister message, with no instruction-limit guard. When the neuron count is large enough, the function traps due to the IC per-message instruction limit. Because it is invoked from `run_periodic_tasks` on every timer tick, it traps again on every subsequent invocation, permanently halting voting reward distribution for all SNS participants. NNS Governance already fixed this exact pattern with a batching mechanism; SNS Governance has no equivalent.

## Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` is called synchronously from `run_periodic_tasks`:

```rust
// rs/sns/governance/src/governance.rs line 5509-5513
if should_distribute_rewards {
    match self.ledger.total_supply().await {
        Ok(supply) => {
            self.distribute_rewards(supply);
        }
```

Inside `distribute_rewards`, two unbounded loops execute in the same message:

**Loop 1 — ballot aggregation (lines 5894–5931):** For every proposal in `considered_proposals` (all `ReadyToSettle` proposals), iterates over every entry in `proposal.ballots`. Each ballot map has one entry per neuron eligible at proposal-creation time. No instruction-limit check exists inside this loop.

**Loop 2 — reward distribution (lines 5954–5997):** Iterates over every entry in `neuron_id_to_reward_shares` (one per neuron that voted across all considered proposals) and mutates each neuron's maturity. Again, no instruction-limit check.

The total work is O(proposals × neurons_per_proposal). There is no intermediate state persistence, no batching, and no break-and-resume mechanism.

By contrast, NNS Governance's `distribute_pending_rewards` in `rs/nns/governance/src/reward/distribution.rs` uses `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` after each neuron update (line 184), breaks out of the loop, and persists remaining work in a `StableBTreeMap`-backed state machine for resumption in the next timer invocation. SNS Governance has no equivalent mechanism — confirmed by the absence of any `is_message_over_threshold` call in `rs/sns/governance/src/governance.rs`.

## Impact Explanation

**Vulnerability class:** Unbounded loop exhausting the per-message instruction limit inside a canister timer task.

**Impact:** Permanent application-level DoS on SNS voting reward distribution. Once the neuron count grows large enough that `distribute_rewards` exceeds the instruction limit, every subsequent timer invocation traps at the same point. No neuron ever receives voting rewards again. All SNS participants who staked tokens and voted are permanently deprived of their maturity increments. SNS canisters run on application subnets, which have lower per-message instruction limits than the system subnet, making SNS governance more susceptible than NNS governance.

This matches the allowed impact: **High — Application/platform-level DoS with concrete user and protocol harm (SNS governance reward distribution permanently halted).**

## Likelihood Explanation

An unprivileged user participates in an SNS token swap from many accounts. One neuron is minted per swap participant. SNS swap parameters allow `max_participant_count` in the tens of thousands for popular launches. Each neuron created during the swap becomes an eligible voter, so every subsequent proposal carries a ballot map with that many entries. The attacker does not need to create proposals — normal governance activity triggers the vulnerable path automatically at the next reward distribution round. The cost is the minimum swap participation amount multiplied by the number of accounts needed to push ballot iteration past the instruction limit. For SNS deployments with low minimum participation thresholds, this is economically feasible and repeatable.

## Recommendation

Apply the same batching pattern already used in NNS Governance:

1. **Short term:** Add an instruction-limit guard inside both loops in `distribute_rewards`, breaking out when the threshold is reached and persisting intermediate state (e.g., remaining `neuron_id_to_reward_shares`) to stable storage for resumption in the next timer invocation — mirroring `RewardsDistribution::continue_processing` in `rs/nns/governance/src/reward/distribution.rs`.

2. **Long term:** Pre-compute each neuron's reward share incrementally at vote-cast time, so that reward distribution at settlement only needs to iterate over neurons that actually voted with the per-neuron share already known, and can be chunked across multiple messages.

## Proof of Concept

1. Deploy an SNS with a low minimum swap participation (e.g., 1 token).
2. Participate in the swap from N accounts (e.g., N = 50,000), creating N neurons.
3. Any neuron holder submits a proposal; it collects N ballots.
4. The proposal's voting period expires; it enters `ReadyToSettle`.
5. The next periodic timer invocation calls `run_periodic_tasks` → `distribute_rewards`.
6. `distribute_rewards` enters the ballot aggregation loop (lines 5894–5931) over N ballots × number of settled proposals. With N = 50,000 and several settled proposals, the instruction count exceeds the application-subnet per-message limit.
7. The canister traps. The timer fires again next round and traps again at the same point.
8. All SNS neurons are permanently denied voting rewards.

Deterministic reproduction: write an integration test or PocketIC test that creates an SNS governance state with 50,000+ neurons, populates `proto.proposals` with one `ReadyToSettle` proposal carrying 50,000 ballots, and calls `distribute_rewards` — observe the instruction counter exceed the application-subnet limit. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5509-5513)
```rust
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
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
