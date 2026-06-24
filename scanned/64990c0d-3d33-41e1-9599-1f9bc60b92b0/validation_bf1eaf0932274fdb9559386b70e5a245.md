### Title
Unbounded Loop in SNS Governance `distribute_rewards` Causes Instruction-Limit DoS - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS Governance canister's `distribute_rewards` function iterates over all `ReadyToSettle` proposals and their full ballot maps in a single synchronous message execution, with no instruction-limit guard. Any unprivileged user who creates neurons and votes on proposals can grow these collections until the function permanently traps on every invocation, halting voting-reward distribution for the entire SNS.

### Finding Description

**Root cause — unbounded nested loop with no instruction-limit check**

`distribute_rewards` is called synchronously from `run_periodic_tasks` (the heartbeat handler). It performs two unbounded loops in sequence:

**Loop 1** — iterates over every `ReadyToSettle` proposal and, for each, over every ballot entry (one per voting neuron): [1](#0-0) 

**Loop 2** — iterates over every neuron that cast a vote to credit maturity: [2](#0-1) 

Neither loop contains any call to `is_message_over_threshold` or any other instruction-limit guard. The total work is proportional to `P × N` where `P` is the number of proposals in `ReadyToSettle` state and `N` is the number of neurons that voted.

**Caller path**

`distribute_rewards` is invoked unconditionally from `run_periodic_tasks` whenever `should_distribute_rewards` returns `true`: [3](#0-2) 

`run_periodic_tasks` is the SNS heartbeat handler, so this executes automatically every round once a reward period elapses.

**Contrast with NNS — the fix already exists there**

The NNS Governance canister went through exactly this problem and was refactored. It now uses a batched state machine (`RewardsDistribution`) with an explicit `is_message_over_threshold` guard that breaks the loop across multiple timer messages: [4](#0-3) 

The NNS CHANGELOG explicitly records this fix: [5](#0-4) 

The SNS `distribute_rewards` has received no equivalent treatment.

**Attacker-controlled growth of the collection**

- Any user can stake SNS tokens and create neurons (up to `max_number_of_neurons`).
- Any neuron holder can vote on every open proposal; each vote adds one entry to `proposal.ballots`.
- Proposals accumulate in `ReadyToSettle` state until the next reward event; GC (`maybe_gc`) only removes proposals that `can_be_purged`, which excludes unsettled proposals. [6](#0-5) 

With `max_number_of_neurons` neurons all voting on `P` proposals, the ballot iteration count is `P × max_number_of_neurons`. At typical IC instruction costs (~2 000 instructions per stable-memory neuron read), even a few hundred neurons across a few dozen proposals can exhaust the 5 billion instruction limit for a single message on an application subnet.

### Impact Explanation

Once the instruction limit is exceeded, `distribute_rewards` traps on every heartbeat invocation. Because the state is rolled back on trap, no progress is ever made: voting rewards are permanently frozen. Neuron holders stop receiving maturity, which undermines the economic incentive to participate in SNS governance. The SNS is effectively bricked for reward distribution without an upgrade.

### Likelihood Explanation

The attack requires no privileged access. Any token holder can stake neurons and vote. The growth is organic — even without a deliberate attacker, a popular SNS with many participants and active governance will naturally accumulate enough proposals and ballots to trigger this. The NNS team already identified and fixed the identical pattern in NNS governance, confirming the realistic likelihood.

### Recommendation

Apply the same batched-distribution pattern already used in NNS governance:

1. In `distribute_rewards`, compute the per-neuron reward shares and store them in a persistent state machine (analogous to `RewardsDistributionStateMachine`).
2. Process the actual maturity credits in a separate periodic timer task that calls `is_message_over_threshold` after each neuron and breaks across messages.
3. Alternatively, add an explicit cap on the number of proposals processed per invocation and carry a cursor across calls, similar to `prune_some_following`. [7](#0-6) 

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons = 10 000`.
2. Have 10 000 users each stake tokens and create a neuron.
3. Submit 50 proposals and have all 10 000 neurons vote on each (500 000 ballot entries total).
4. Allow the proposals' voting periods to expire so they enter `ReadyToSettle`.
5. Wait for the next reward period to elapse.
6. Observe that `run_periodic_tasks` → `distribute_rewards` traps with `CanisterInstructionLimitExceeded` on every heartbeat.
7. Voting rewards are never distributed again without a canister upgrade.

### Citations

**File:** rs/sns/governance/src/governance.rs (L5395-5468)
```rust
    pub fn maybe_gc(&mut self) -> bool {
        let now_seconds = self.env.now();
        // Run GC if either (a) more than 24 hours have passed since it
        // was run last, or (b) more than 100 proposals have been
        // added since it was run last.
        if !(now_seconds > self.latest_gc_timestamp_seconds + 60 * 60 * 24
            || self.proto.proposals.len() > self.latest_gc_num_proposals + 100)
        {
            // Condition to run was not met. Return false.
            return false;
        }
        self.latest_gc_timestamp_seconds = self.env.now();

        log!(
            INFO,
            "Running GC now at {}.",
            format_timestamp_for_humans(self.latest_gc_timestamp_seconds),
        );

        let max_proposals_to_keep_per_action = match self
            .nervous_system_parameters()
            .and_then(|params| params.max_proposals_to_keep_per_action)
        {
            None => {
                log!(
                    ERROR,
                    "NervousSystemParameters must have max_proposals_to_keep_per_action"
                );
                return false;
            }
            Some(max) => max as usize,
        };

        // This data structure contains proposals grouped by action.
        //
        // Proposals are stored in order based on ProposalId, where ProposalIds are assigned in
        // order of creation in the governance canister (i.e. chronologically). The following
        // data structure maintains the same chronological order for proposals in each action's
        // vector.
        let action_to_proposals: HashMap<u64, Vec<u64>> = {
            let mut tmp: HashMap<u64, Vec<u64>> = HashMap::new();
            for (proposal_id, proposal) in self.proto.proposals.iter() {
                tmp.entry(proposal.action).or_default().push(*proposal_id);
            }
            tmp
        };
        // Only keep the latest 'max_proposals_to_keep_per_action'. This is a soft maximum
        // as garbage collection cannot purge un-finalized proposals, and only a subset of proposals
        // at the head of the list are examined.
        // TODO NNS1-1259: Improve "best-effort" garbage collection of proposals
        for (proposal_action, proposals_of_action) in action_to_proposals {
            log!(
                INFO,
                "GC - proposal_type {:#?} max {} current {}",
                proposal_action,
                max_proposals_to_keep_per_action,
                proposals_of_action.len()
            );
            if proposals_of_action.len() > max_proposals_to_keep_per_action {
                for proposal_id in proposals_of_action
                    .iter()
                    .take(proposals_of_action.len() - max_proposals_to_keep_per_action)
                {
                    // Check that this proposal can be purged.
                    if let Some(proposal) = self.proto.proposals.get(proposal_id)
                        && proposal.can_be_purged(now_seconds)
                    {
                        self.proto.proposals.remove(proposal_id);
                    }
                }
            }
        }
        self.latest_gc_num_proposals = self.proto.proposals.len();
        true
```

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

**File:** rs/nns/governance/CHANGELOG.md (L655-670)
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
* Voting Rewards will be distributed asynchronously in the background after being calculated.
    * This will allow rewards to be compatible with neurons being stored in Stable Memory.
```

**File:** rs/nns/governance/src/timer_tasks/prune_following.rs (L43-57)
```rust
impl RecurringSyncTask for PruneFollowingTask {
    fn execute(self) -> (Duration, Self) {
        let new_begin = self.governance.with_borrow_mut(|governance| {
            let carry_on = || !is_message_over_threshold(MAX_PRUNE_SOME_FOLLOWING_INSTRUCTIONS);
            governance.prune_some_following(self.begin, carry_on)
        });

        (
            PRUNE_FOLLOWING_INTERVAL,
            Self {
                governance: self.governance,
                begin: new_begin,
            },
        )
    }
```
