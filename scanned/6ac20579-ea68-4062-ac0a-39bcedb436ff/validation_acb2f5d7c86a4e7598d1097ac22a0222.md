### Title
Unbounded Iteration Over All Proposal Ballots in SNS Governance `distribute_rewards` Causes Permanent Denial of Service on Voting Reward Distribution - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS Governance canister's `distribute_rewards` function iterates over all ballots of all ready-to-settle proposals and all neurons that voted, in a single message execution, with no instruction-limit check and no batching mechanism. When the number of neurons (and thus ballots per proposal) is large enough, this function will trap due to the IC instruction limit, permanently preventing voting reward distribution for all SNS participants.

---

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` performs two unbounded loops in a single canister message:

**Loop 1 — ballot aggregation (lines 5894–5930):** For every proposal in `considered_proposals` (all proposals in `ReadyToSettle` state), it iterates over every entry in `proposal.ballots`. Each ballot map contains one entry per neuron that was eligible at proposal-creation time. There is no instruction-limit guard inside this loop.

```rust
for proposal_id in &considered_proposals {
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots {
            // ... accumulate neuron_id_to_reward_shares
        }
    }
}
```

**Loop 2 — reward distribution (lines 5954–5997):** Iterates over every entry in `neuron_id_to_reward_shares` (one entry per neuron that voted across all considered proposals) and mutates each neuron's maturity. Again, no instruction-limit guard.

```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron = match self.get_neuron_result_mut(&neuron_id) { ... };
    // ... update neuron.maturity_e8s_equivalent
}
```

The total work is **O(proposals × neurons\_per\_proposal)**. Because `distribute_rewards` is invoked from the canister's periodic timer task (`run_periodic_tasks`), if it traps due to instruction exhaustion the timer fires again next round and traps again — indefinitely.

By contrast, the NNS Governance canister explicitly recognized and fixed this exact pattern. Its CHANGELOG states:

> *"Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages."*
> *"Unstaking maturity task has a limit of 100 neurons per message, which prevents it from exceeding instruction limit."*

NNS Governance now uses `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` inside `continue_processing` to break out of the loop and resume in the next timer invocation. SNS Governance has no equivalent mechanism. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

**Vulnerability class:** Cycles/resource accounting bug — unbounded loop exhausts the per-message instruction limit inside a canister timer task.

**Impact:** Permanent denial of service on SNS voting reward distribution. Once the neuron count grows large enough that `distribute_rewards` exceeds the instruction limit, every subsequent timer invocation traps at the same point. No neuron ever receives voting rewards again. Neurons that voted on proposals are permanently deprived of their maturity increments. This is a direct financial loss to all SNS participants who staked tokens and voted.

SNS canisters run on **application subnets**, which have lower per-message instruction limits than the system subnet where NNS canisters run, making SNS governance more susceptible to this exhaustion. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

**Attacker-controlled entry path:** An unprivileged user participates in an SNS token swap using many accounts (one neuron is minted per swap participant). The SNS swap parameters set `max_participant_count`, but this can be in the tens of thousands for popular SNS launches. Each neuron created during the swap becomes an eligible voter, so every subsequent proposal carries a ballot map with that many entries.

The attacker does not need to create proposals themselves — they only need to ensure enough neurons exist. Normal governance activity (proposals created by any neuron holder) then triggers the vulnerable path automatically at the next reward distribution round.

The cost to the attacker is the minimum swap participation amount multiplied by the number of accounts needed to push ballot iteration past the instruction limit. For SNS deployments with low minimum participation thresholds, this is economically feasible. [7](#0-6) [8](#0-7) 

---

### Recommendation

Apply the same batching pattern already used in NNS Governance:

1. **Short term:** Add an instruction-limit guard inside both loops in `distribute_rewards`, breaking out when the threshold is reached and persisting intermediate state (e.g., remaining `neuron_id_to_reward_shares`) to stable storage for resumption in the next timer invocation — mirroring `RewardsDistribution::continue_processing` in NNS Governance.

2. **Long term:** Pre-compute each neuron's reward share at vote-cast time (incrementally), so that reward distribution at settlement only needs to iterate over neurons that actually voted, with the per-neuron share already known, and can be chunked across multiple messages. [9](#0-8) [6](#0-5) 

---

### Proof of Concept

1. Deploy an SNS with a low minimum swap participation (e.g., 1 token).
2. Participate in the swap from N accounts (e.g., N = 50,000), creating N neurons.
3. Any neuron holder submits a proposal; it collects N ballots.
4. The proposal's voting period expires; it enters `ReadyToSettle`.
5. The next periodic timer invocation calls `run_periodic_tasks` → `distribute_rewards`.
6. `distribute_rewards` enters the ballot aggregation loop over N ballots × number of settled proposals. With N = 50,000 and several settled proposals, the instruction count exceeds the application-subnet per-message limit.
7. The canister traps. The timer fires again next round and traps again.
8. All SNS neurons are permanently denied voting rewards.

The root cause is confirmed at:

- `rs/sns/governance/src/governance.rs` lines 5894–5930 (ballot aggregation, no limit guard)
- `rs/sns/governance/src/governance.rs` lines 5954–5997 (reward distribution, no limit guard)

compared to the fixed NNS path at:

- `rs/nns/governance/src/reward/distribution.rs` lines 159–187 (`is_over_instructions_limit()` checked after each neuron) [10](#0-9) [2](#0-1) [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5763-5764)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
```

**File:** rs/sns/governance/src/governance.rs (L5822-5823)
```rust
        let considered_proposals: Vec<ProposalId> =
            self.ready_to_be_settled_proposal_ids().collect();
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

**File:** rs/nns/governance/CHANGELOG.md (L655-668)
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
```
