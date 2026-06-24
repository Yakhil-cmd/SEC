### Title
Unbounded Iteration in SNS Governance `distribute_rewards` Over All Proposals × Neuron Ballots Without Instruction-Limit Guard - (File: rs/sns/governance/src/governance.rs)

---

### Summary

`SNS Governance::distribute_rewards` iterates synchronously over all `ReadyToSettle` proposals and all neuron ballots within each proposal, then over all voting neurons again to apply maturity, with no instruction-limit check at any point. With up to `MAX_NUMBER_OF_NEURONS_CEILING = 200,000` neurons and `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING = 700` proposals, the function can exhaust the IC per-message instruction limit (~40 billion instructions), causing the periodic timer to trap on every invocation and permanently breaking SNS reward distribution.

---

### Finding Description

`distribute_rewards` is a synchronous function called from `run_periodic_tasks` (which is itself called from a repeating timer). It contains four unbounded loops with no instruction-limit guard:

1. **Round accumulation loop** (line 5861): `for i in 1..=new_rounds_count` — iterates over all missed reward rounds, each performing `Decimal` arithmetic.

2. **Ballot aggregation double-loop** (lines 5894–5930): `for proposal_id in &considered_proposals` → `for (voter, ballot) in &proposal.ballots` — iterates over every ballot of every `ReadyToSettle` proposal. In the worst case this is `700 proposals × 200,000 neurons = 140,000,000` iterations, each performing `Decimal` arithmetic and `HashMap` insertion.

3. **Maturity distribution loop** (lines 5954–5997): `for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares` — iterates over all voting neurons (up to 200,000), performing `Decimal` division and neuron mutation.

4. **Proposal settlement loop** (line 6013): `for pid in &considered_proposals` — calls `self.process_proposal(pid.id)` for each settled proposal.

A `grep` over the entire `rs/sns/governance/**/*.rs` tree confirms there is **zero** use of `is_message_over_threshold`, `instruction_counter`, or any equivalent guard in SNS governance.

By contrast, NNS governance explicitly fixed this class of bug. The NNS CHANGELOG (Proposal 135702) states: *"Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages."* NNS governance's `distribute_pending_rewards` uses `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` to break out of the loop and resume in the next timer tick.

---

### Impact Explanation

When the instruction limit is exceeded, the IC runtime traps the message. Because `distribute_rewards` is called inside the timer callback `run_periodic_tasks`, the trap causes the entire periodic task to fail silently. The timer is re-scheduled and will trap again on the next invocation with the same state, permanently preventing:

- Voting reward maturity from being credited to any neuron.
- Proposals from being moved from `ReadyToSettle` to `Settled`, which blocks ballot cleanup and eventually blocks new proposals (since `max_number_of_proposals_with_ballots` is enforced).

This is a **governance availability / cycles-resource accounting bug** that can render an SNS governance canister unable to distribute rewards indefinitely.

---

### Likelihood Explanation

Any unprivileged principal who holds SNS tokens can stake them and create neurons. The `max_number_of_neurons` governance parameter has a ceiling of `200,000` and a default of `200,000`. The `max_number_of_proposals_with_ballots` ceiling is `700`. A large, active SNS (e.g., one with tens of thousands of neurons and many proposals settling in the same reward round) can reach the instruction limit organically without any deliberate attack. A motivated actor can accelerate this by staking many small neurons and submitting many proposals.

---

### Recommendation

Apply the same batched-processing pattern already used in NNS governance:

1. Move the maturity-distribution step into a resumable state machine (analogous to `RewardsDistributionStateMachine` in `rs/nns/governance/src/reward/distribution.rs`).
2. Check `is_message_over_threshold` after each neuron is processed and break out of the loop, persisting remaining work to stable storage.
3. Re-run the distribution in subsequent timer ticks until all neurons have been credited.
4. Alternatively, impose a hard cap on the number of neurons that can be processed per `distribute_rewards` invocation and document the maximum safe `max_number_of_neurons × max_number_of_proposals_with_ballots` product.

---

### Proof of Concept

**Root cause — no instruction guard in `distribute_rewards`:** [1](#0-0) 

**Ballot aggregation double-loop (proposals × neurons, no guard):** [2](#0-1) 

**Maturity distribution loop (all voting neurons, no guard):** [3](#0-2) 

**Called unconditionally from the periodic timer:** [4](#0-3) 

**Ceiling constants that bound the worst-case iteration count:** [5](#0-4) 

**NNS governance fix (instruction-limit guard in the analogous loop):** [6](#0-5) 

**NNS CHANGELOG confirming the fix was applied specifically to address this class of bug:** [7](#0-6)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L5763-5764)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
```

**File:** rs/sns/governance/src/governance.rs (L5893-5930)
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

**File:** rs/sns/governance/src/types.rs (L386-390)
```rust
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;

    /// This is an upper bound for `max_number_of_proposals_with_ballots`. Exceeding
    /// it may cause degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING: u64 = 700;
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

**File:** rs/nns/governance/CHANGELOG.md (L654-658)
```markdown
    * Compared to the last time it was enabled, several improvements were made:
        * Distribute rewards is moved to timer, and has a mechanism to distribute in batches in
          multiple messages.
        * Unstaking maturity task has a limit of 100 neurons per message, which prevents it from
          exceeding instruction limit.
```
