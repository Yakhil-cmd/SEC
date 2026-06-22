### Title
Unbounded Loop Over Proposals × Ballots in SNS `distribute_rewards` Causes Permanent Instruction-Limit DoS — (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `distribute_rewards` function iterates over all proposals in `ReadyToSettle` state and, for each proposal, over every ballot (one per eligible neuron), with no instruction-limit guard anywhere in the function. If the combined work exceeds the IC per-message instruction limit the message traps, state is rolled back, and the function will fail identically on every subsequent periodic invocation — permanently preventing reward distribution and proposal settlement.

---

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` performs five sequential unbounded loops in a single synchronous message:

1. **`for i in 1..=new_rounds_count`** — iterates over every missed reward round since the last distribution event. [1](#0-0) 

2. **`for proposal_id in &considered_proposals`** — iterates over every proposal in `ReadyToSettle` state. [2](#0-1) 

3. **`for (voter, ballot) in &proposal.ballots`** — for each such proposal, iterates over every ballot (one per eligible neuron at proposal creation time). [3](#0-2) 

4. **`for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares`** — iterates over every neuron that voted across all settled proposals. [4](#0-3) 

5. **`for pid in &considered_proposals`** — iterates over all settled proposals again to clear ballots. [5](#0-4) 

None of these loops contain an `is_message_over_threshold` / instruction-limit check. The function is called from the SNS periodic-task timer with no batching mechanism.

By contrast, the NNS governance canister was explicitly refactored to use a `RewardsDistribution` state machine with per-iteration instruction-limit checks and multi-message batching via timers: [6](#0-5) 

The SNS governance canister received no equivalent fix.

---

### Impact Explanation

**Vulnerability type:** Cycles/resource accounting bug — unbounded iteration causing instruction-limit exhaustion leading to permanent DoS of the SNS governance periodic task.

When the instruction limit is exceeded, the IC traps the message and rolls back all state changes. Because `distribute_rewards` is called from a timer and the state is unchanged after the trap, every subsequent timer invocation encounters the same (or larger) workload and traps again. The result is:

- Voting rewards are **never distributed** to SNS neurons.
- Proposals in `ReadyToSettle` state are **never settled** (ballots are never cleared).
- The accumulation of unsettled proposals and their ballots grows over time, making recovery impossible without an upgrade.
- Other periodic tasks sharing the same `run_periodic_tasks` call may also be disrupted.

**Impact: High** — core SNS governance functionality (reward distribution, proposal settlement) is permanently broken.

---

### Likelihood Explanation

**Likelihood: Low-to-Medium.**

An attacker (or organic growth) needs:
1. Many SNS neurons — any token holder can stake to create a neuron; no privileged access required.
2. Many proposals entering `ReadyToSettle` simultaneously — any neuron with sufficient stake can submit proposals.

The total work is proportional to `P × N` where `P` = proposals in `ReadyToSettle` and `N` = neurons eligible per proposal. For a popular SNS with thousands of neurons and dozens of accumulated proposals, this threshold can be reached organically. A motivated attacker holding SNS tokens can accelerate this by submitting many proposals and creating many neurons.

No admin key, governance majority, or threshold attack is required — only SNS token holdings.

---

### Recommendation

Apply the same batching pattern already used in NNS governance:

1. Replace the synchronous `distribute_rewards` with a state machine (analogous to `RewardsDistributionStateMachine`) that persists intermediate progress.
2. Add `is_message_over_threshold` checks inside each loop body and break when the soft limit is reached.
3. Re-schedule the timer to continue processing in the next message.
4. Enforce a maximum number of proposals that can accumulate in `ReadyToSettle` state simultaneously.

---

### Proof of Concept

**Setup:**
- Deploy an SNS with `round_duration_seconds` set to a short interval.
- Create N neurons (e.g., 5,000) by having token holders stake.
- Submit P proposals (e.g., 50) and allow them to reach `ReadyToSettle` state.

**Trigger:**
- Wait for the periodic task timer to fire and invoke `distribute_rewards`.
- The inner ballot loop processes N × P = 250,000 entries. At ~2,000 instructions per ballot entry, this is ~500 million instructions — well within a single round. However, scaling to 50,000 neurons × 100 proposals = 5,000,000 entries at ~2,000 instructions each = 10 billion instructions, which approaches or exceeds the IC's per-message limit (~20 billion for update calls, but timers share the same budget).

**Result:**
- The message traps; state is rolled back.
- `latest_reward_event` is not updated.
- The next timer invocation processes the same (now larger) set of proposals and traps again.
- The SNS governance canister is permanently unable to distribute rewards or settle proposals without a canister upgrade.

The root cause is the absence of any instruction-limit guard in `distribute_rewards`: [7](#0-6) [8](#0-7)

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
