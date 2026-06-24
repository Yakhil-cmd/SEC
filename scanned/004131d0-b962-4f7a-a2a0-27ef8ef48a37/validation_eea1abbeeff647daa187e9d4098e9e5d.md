### Title
SNS Governance `distribute_rewards` Iterates Over All Voting Neurons Without Instruction Limit, Enabling Permanent DoS on Reward Distribution - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance `distribute_rewards` function iterates over every neuron that voted on settled proposals in a single unbounded loop with no instruction-limit guard. Any unprivileged principal who stakes enough SNS tokens to fill the neuron roster can force this loop to exhaust the IC per-message instruction budget, permanently trapping the periodic reward-distribution task and halting all future maturity accrual for the SNS.

---

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` builds a `neuron_id_to_reward_shares` map from every ballot on every `ReadyToSettle` proposal, then iterates over the entire map to credit maturity:

```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) { … };
    …
    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
    distributed_e8s_equivalent += neuron_reward_e8s;
}
``` [1](#0-0) 

There is no `is_message_over_threshold` check, no batching, and no early-exit path. The `latest_reward_event` update and ballot-clearing happen **after** this loop: [2](#0-1) 

`distribute_rewards` is invoked synchronously inside `run_periodic_tasks`, which is the SNS governance timer callback: [3](#0-2) 

The number of entries in `neuron_id_to_reward_shares` equals the number of neurons that held a ballot on the settled proposal. Ballots are created for every eligible neuron at proposal-submission time via `compute_ballots_for_new_proposal`, so the loop size is bounded only by `max_number_of_neurons`.

By contrast, the NNS governance already recognised this exact hazard and migrated reward distribution to a batched, instruction-checked timer task: [4](#0-3) 

The NNS CHANGELOG explicitly records the fix: [5](#0-4) 

The SNS governance has no equivalent protection.

---

### Impact Explanation

If the loop exhausts the instruction budget the canister traps and the entire message is rolled back. Because `latest_reward_event` is written only after the loop, the SNS governance's `should_distribute_rewards` predicate will return `true` again on the next timer tick, re-entering the same loop with the same (uncleared) ballots and the same neuron count. The trap repeats indefinitely, permanently halting maturity accrual for every neuron in the SNS. No governance proposal can fix this without an upgrade; the SNS loses its core economic incentive mechanism.

---

### Likelihood Explanation

The attack requires staking enough SNS tokens to create neurons up to the `max_number_of_neurons` ceiling. Once neurons are created they can be set to follow a single neuron via `Follow`, so a single vote automatically propagates to all of them. The NNS governance benchmarks show that iterating over ~100 neurons in a reward-distribution context costs ~2.9 M instructions: [6](#0-5) 

Scaling linearly, reaching the 5 B instruction limit for a timer message requires roughly 170 K neurons. SNS instances with high `max_number_of_neurons` ceilings (the proto allows governance to set this value) are directly in range. A well-funded attacker or a colluding token-holder majority can reach this threshold.

---

### Recommendation

Apply the same batched-distribution pattern the NNS governance already uses:

1. After computing `neuron_id_to_reward_shares`, persist it to stable storage (analogous to `RewardsDistributionInProgress`).
2. Update `latest_reward_event` and clear ballots immediately (before distributing), so the reward period is not re-entered.
3. Distribute maturity in a separate periodic timer task that calls `is_message_over_threshold` after each neuron and resumes in the next message if the limit is reached. [7](#0-6) 

---

### Proof of Concept

1. Stake SNS tokens to create N neurons (N approaching `max_number_of_neurons`).
2. Call `manage_neuron { Follow … }` on each neuron to follow a single controller neuron.
3. Submit any proposal eligible for voting rewards; all N neurons receive ballots automatically via cascading follow.
4. Vote `Yes` with the controller neuron; all N neurons inherit the vote.
5. Wait for the proposal's voting period to expire (`ReadyToSettle`).
6. On the next `run_periodic_tasks` timer tick, `distribute_rewards` enters the N-entry loop.
7. The loop exhausts the instruction budget; the canister traps; state rolls back.
8. `latest_reward_event` is unchanged; `should_distribute_rewards` returns `true` again.
9. Every subsequent timer tick repeats steps 6–8; reward distribution is permanently broken. [8](#0-7) [9](#0-8)

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

**File:** rs/sns/governance/src/governance.rs (L5892-5998)
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
        // Freeze reward shares, now that we are done adding them up.
        let neuron_id_to_reward_shares = neuron_id_to_reward_shares;
        let total_reward_shares: Decimal = neuron_id_to_reward_shares.values().sum();
        debug_assert!(
            total_reward_shares >= dec!(0),
            "total_reward_shares: {total_reward_shares} neuron_id_to_reward_shares: {neuron_id_to_reward_shares:#?}",
        );

        // Because of rounding (and other shenanigans), it is possible that some
        // portion of this amount ends up not being actually distributed.
        let mut distributed_e8s_equivalent = 0_u64;
        // Now that we know the size of the pie (rewards_purse_e8s), and how
        // much of it each neuron is supposed to get (*_reward_shares), we now
        // proceed to actually handing out those rewards.
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
        } else {
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
        }
```

**File:** rs/sns/governance/src/governance.rs (L5999-6030)
```rust
        // Freeze distributed_e8s_equivalent, now that we are done handing out rewards.
        let distributed_e8s_equivalent = distributed_e8s_equivalent;
        // Because we used floor to round rewards to integers (and everything is
        // non-negative), it should be that the amount distributed is not more
        // than the original purse.
        debug_assert!(
            i2d(distributed_e8s_equivalent) <= rewards_purse_e8s,
            "rewards distributed ({distributed_e8s_equivalent}) > purse ({rewards_purse_e8s})",
        );

        // This field is deprecated. People should really use end_timestamp_seconds
        // instead. This value can still be used if round duration is not changed.
        let new_reward_event_round = self.latest_reward_event().round + new_rounds_count;
        // Settle proposals.
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
```

**File:** rs/nns/governance/src/reward/distribution.rs (L41-52)
```rust
    // Returns if there is work left to do
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

**File:** rs/nns/governance/CHANGELOG.md (L655-669)
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
```

**File:** rs/nns/governance/canbench/canbench_results.yml (L51-57)
```yaml
  distribute_rewards_with_stable_neurons:
    total:
      calls: 1
      instructions: 2867879
      heap_increase: 0
      stable_memory_increase: 256
    scopes: {}
```
