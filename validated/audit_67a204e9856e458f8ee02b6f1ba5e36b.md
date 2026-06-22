### Title
SNS Governance `distribute_rewards` Iterates Over Unbounded Neuron/Ballot List in a Single Message Execution - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance canister's `distribute_rewards` function processes all proposal ballots and distributes maturity to all rewarded neurons in a single synchronous message execution with no instruction-limit check or batching. With up to `MAX_NUMBER_OF_NEURONS_CEILING = 200,000` neurons and up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING = 700` proposals settling in one round, the function can exceed the IC's per-message instruction limit (40 billion instructions), causing the message to trap. Because `latest_reward_event` is only written at the very end of the function, a trap rolls back all state, and every subsequent `run_periodic_tasks` invocation re-attempts the same failing computation, permanently blocking reward distribution and all other periodic tasks in the SNS governance canister.

### Finding Description

`distribute_rewards` in the SNS governance canister performs two unbounded loops inside a single synchronous call:

**Loop 1 — ballot aggregation** (lines 5894–5930): iterates over every `considered_proposals` entry and, for each proposal, over every `(voter, ballot)` pair in `proposal.ballots`. In the worst case this is `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING × MAX_NUMBER_OF_NEURONS_CEILING = 700 × 200,000 = 140,000,000` iterations, each performing a `HashMap` insert/update. [1](#0-0) 

**Loop 2 — maturity distribution** (lines 5954–5997): iterates over every entry in `neuron_id_to_reward_shares` (up to 200,000 neurons) and mutates each neuron's `maturity_e8s_equivalent` or `staked_maturity_e8s_equivalent`. [2](#0-1) 

Neither loop contains any instruction-limit check or early-exit mechanism. The entire function is called synchronously from `run_periodic_tasks`: [3](#0-2) 

`latest_reward_event` is only written at the very end of the function, after both loops complete: [4](#0-3) 

If the message traps before reaching that write, the IC rolls back all state changes. `should_distribute_rewards` will return `true` again on the next timer tick (because `latest_reward_event` was never updated), causing the same failing computation to be retried indefinitely. [5](#0-4) 

**Contrast with NNS governance**, which recognized this exact problem and introduced a `RewardsDistributionStateMachine` that processes rewards in batches across multiple messages, checking `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` after each neuron: [6](#0-5) 

The NNS CHANGELOG explicitly documents this fix: *"Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages."* [7](#0-6) 

The SNS governance canister has no equivalent mechanism.

### Impact Explanation

When the instruction limit is exceeded, the IC traps the entire `run_periodic_tasks` message. Because `run_periodic_tasks` also handles upgrade checks, maturity finalization (`maybe_finalize_disburse_maturity`), and staked-maturity movement (`maybe_move_staked_maturity`), all of these tasks are also blocked: [8](#0-7) 

The result is a permanent liveness failure of the SNS governance canister: voting rewards are never distributed, upgrade proposals cannot be finalized, and maturity disbursements stall. The SNS community cannot recover without an emergency canister upgrade.

**Impact: Medium** — Reward distribution is permanently blocked; all SNS periodic tasks fail; maturity and upgrade finalization are halted.

### Likelihood Explanation

`MAX_NUMBER_OF_NEURONS_CEILING = 200,000` is the hard ceiling enforced by `validate_max_number_of_neurons`: [9](#0-8) 

A mature SNS with a large community (e.g., post-swap with many direct participants) can reach tens of thousands of neurons. The `max_number_of_proposals_with_ballots` ceiling is 700. Even at 50,000 neurons and 10 proposals settling simultaneously, the ballot loop performs 500,000 `HashMap` operations — a realistic scenario for a popular SNS. No attacker action is required; normal protocol operation (users staking neurons, proposals being submitted and settling) drives the system toward this state.

**Likelihood: Medium**

### Recommendation

Apply the same batched-distribution pattern used by NNS governance to SNS governance:

1. After computing `neuron_id_to_reward_shares`, persist it to stable storage (analogous to `RewardsDistributionInProgress`).
2. Update `latest_reward_event` immediately so the round is marked as settled and the computation is not retried.
3. Distribute maturity to neurons in a separate periodic timer task that checks `is_message_over_threshold` after each neuron and resumes in subsequent messages until the distribution is complete.

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons = 200_000` and `max_number_of_proposals_with_ballots = 700`.
2. Have 200,000 users stake neurons and vote on 700 proposals.
3. Wait for the reward round to end. `run_periodic_tasks` calls `distribute_rewards`.
4. `distribute_rewards` enters the ballot-aggregation loop (140,000,000 `HashMap` operations) and the maturity-distribution loop (200,000 neuron mutations) in a single message.
5. The message exceeds the IC's 40-billion-instruction limit and traps.
6. `latest_reward_event` is not updated; `should_distribute_rewards` returns `true` on the next tick.
7. Every subsequent `run_periodic_tasks` invocation traps at the same point — permanently blocking all SNS periodic tasks. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5471-5534)
```rust
    /// Runs periodic tasks that are not directly triggered by user input.
    pub async fn run_periodic_tasks(&mut self) {
        use ic_cdk::println;

        self.process_proposals();

        // None of the upgrade-related tasks should interleave with one another or themselves, so we acquire a global
        // lock for the duration of their execution. This will return `false` if the lock has already been acquired less
        // than 10 minutes ago by a previous invocation of `run_periodic_tasks`, in which case we skip the
        // upgrade-related tasks.
        if self.acquire_upgrade_periodic_task_lock() {
            // We only want to check the upgrade status if we are currently executing an upgrade.
            if self.should_check_upgrade_status() {
                self.check_upgrade_status().await;
            }

            if self.should_refresh_cached_upgrade_steps() {
                match self.try_temporarily_lock_refresh_cached_upgrade_steps() {
                    Err(err) => {
                        log!(ERROR, "{}", err);
                    }
                    Ok(deployed_version) => {
                        self.refresh_cached_upgrade_steps(deployed_version).await;
                    }
                }
            }

            self.initiate_upgrade_if_sns_behind_target_version().await;

            self.release_upgrade_periodic_task_lock();
        }

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

        if self.should_update_maturity_modulation() {
            self.update_maturity_modulation().await;
        }

        self.maybe_finalize_disburse_maturity().await;

        self.maybe_move_staked_maturity();

        self.compute_cached_metrics().await;

        self.maybe_gc();
    }
```

**File:** rs/sns/governance/src/governance.rs (L5725-5753)
```rust
    fn should_distribute_rewards(&self) -> bool {
        let now = self.env.now();

        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            None => return false,
            Some(ok) => ok,
        };
        let seconds_since_last_reward_event = now.saturating_sub(
            self.latest_reward_event()
                .end_timestamp_seconds
                .unwrap_or_default(),
        );

        let round_duration_seconds = match voting_rewards_parameters.round_duration_seconds {
            Some(s) => s,
            None => {
                log!(
                    ERROR,
                    "round_duration_seconds unset:\n{:#?}",
                    voting_rewards_parameters,
                );
                return false;
            }
        };

        seconds_since_last_reward_event > round_duration_seconds
```

**File:** rs/sns/governance/src/governance.rs (L5763-5765)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
        let now = self.env.now();
```

**File:** rs/sns/governance/src/governance.rs (L5892-5931)
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

**File:** rs/sns/governance/src/governance.rs (L6084-6093)
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
