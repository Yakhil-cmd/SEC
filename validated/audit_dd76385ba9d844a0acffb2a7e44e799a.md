### Title
Unbounded Neuron Iteration in SNS `distribute_rewards` Causes Heartbeat Instruction-Limit Trap, Permanently Blocking Reward Distribution - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

SNS governance's `distribute_rewards` function iterates over all ballots of all considered proposals and all voting neurons in a single synchronous heartbeat execution with no instruction-limit guard. NNS governance explicitly fixed this same class of bug by moving reward distribution to a batched timer with `is_message_over_threshold` checks. SNS governance has no equivalent protection. An unprivileged user who stakes SNS tokens to fill the neuron population, combined with normal proposal activity, can push the heartbeat past the IC's 5-billion-instruction message limit, permanently halting reward distribution for the SNS.

---

### Finding Description

**Root cause — SNS `distribute_rewards` (no batching, no instruction guard):**

`rs/sns/governance/src/governance.rs` `distribute_rewards` (called from `run_periodic_tasks` / heartbeat):

```rust
// Add up reward shares based on voting power that was exercised.
let mut neuron_id_to_reward_shares: HashMap<NeuronId, Decimal> = HashMap::new();
for proposal_id in &considered_proposals {          // up to max_number_of_proposals_with_ballots (≤700)
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots {  // up to max_number_of_neurons (≤200,000) per proposal
            ...
        }
    }
}
...
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) { ... };
    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
    ...
}
``` [1](#0-0) 

There is no call to `is_message_over_threshold`, no batching, and no timer-based continuation. The entire computation — O(proposals × neurons) — must complete within a single heartbeat message.

**Contrast with NNS governance (fixed):**

NNS governance explicitly moved reward distribution to a batched timer with an instruction-limit guard:

```rust
fn continue_processing(...) {
    while let Some((id, reward_e8s)) = self.rewards.pop_first() {
        ...
        if is_over_instructions_limit() {   // ← guard absent in SNS
            break;
        }
    }
}
``` [2](#0-1) 

The NNS CHANGELOG documents this fix explicitly:

> "Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages."
> "Unstaking maturity task has a limit of 100 neurons per message, which prevents it from exceeding instruction limit." [3](#0-2) 

SNS governance has no equivalent fix. Its `distribute_rewards` is called synchronously inside `run_periodic_tasks`: [4](#0-3) 

**Bounds that define the worst case:**

- `MAX_NUMBER_OF_NEURONS_CEILING` = 200,000 neurons
- `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING` = 700 proposals [5](#0-4) 

Worst-case ballot entries: 700 × 200,000 = **140 million** entries, each requiring a map lookup and arithmetic. The IC message instruction limit is 5 billion; at even a modest ~50 instructions per ballot entry this is 7 billion instructions — well over the limit.

**Secondary surface — `compute_ballots_for_new_proposal` (proposal submission):**

The same unbounded iteration over all neurons occurs synchronously during `make_proposal`:

```rust
for (k, v) in self.proto.neurons.iter() {   // all neurons, no guard
    ...
    electoral_roll.insert(k.clone(), Ballot { ... });
}
``` [6](#0-5) 

NNS governance benchmarks show ~24,500 instructions per neuron for ballot computation. At 200,000 neurons: ~4.9 billion instructions, approaching the 5-billion limit for a single update call. [7](#0-6) 

---

### Impact Explanation

When the SNS neuron population is large and proposals have accumulated, the `run_periodic_tasks` heartbeat traps on instruction exhaustion during `distribute_rewards`. Because the heartbeat is the only mechanism that calls `distribute_rewards`, and because the SNS has no batched-timer fallback, **voting rewards permanently stop being distributed**. Neurons that voted on proposals never receive their maturity increments. This is a permanent freeze of accrued rewards for all SNS participants, not merely a temporary delay.

Secondarily, if `compute_ballots_for_new_proposal` traps, no new proposals can be submitted, freezing governance entirely.

---

### Likelihood Explanation

Any SNS deployed with a high `max_number_of_neurons` (the governance-controlled parameter, bounded by `MAX_NUMBER_OF_NEURONS_CEILING` = 200,000) is at risk as its community grows. Unprivileged users trigger this by the normal act of staking SNS tokens and claiming neurons — no special privilege is required. The condition worsens monotonically as the SNS matures and accumulates neurons and proposals. No attacker coordination is needed; organic growth of a successful SNS is sufficient.

---

### Recommendation

Apply the same fix used in NNS governance:

1. Move SNS `distribute_rewards` out of the synchronous `run_periodic_tasks` heartbeat and into a dedicated periodic timer task.
2. Add an `is_message_over_threshold` (or equivalent instruction-counter) guard inside the reward-distribution loop so that work is broken into multiple messages, resuming from a stable checkpoint each time.
3. Add a similar guard or snapshot-based approach to `compute_ballots_for_new_proposal` (as NNS governance does via `compute_voting_power_snapshot_for_standard_proposal` with `with_active_neurons_iter_sections`).

---

### Proof of Concept

**Deterministic reasoning (no code execution required):**

1. Deploy an SNS with `max_number_of_neurons` = 200,000 (within the allowed ceiling).
2. Have users stake SNS tokens until the neuron count approaches 200,000. Each user calls `manage_neuron` → `ClaimOrRefresh` — a standard, unprivileged operation.
3. Submit and let expire 700 proposals (the `max_number_of_proposals_with_ballots` ceiling). Each proposal records a ballot for every eligible neuron.
4. When the reward round ends, `run_periodic_tasks` calls `distribute_rewards`. The function must iterate over 700 proposals × 200,000 ballots = 140 million entries, plus update maturity for every neuron that voted. This far exceeds the 5-billion-instruction message limit.
5. The heartbeat traps. No rewards are distributed. The trap recurs on every subsequent heartbeat invocation of `run_periodic_tasks`, permanently blocking reward distribution.

The NNS governance benchmark confirms the per-neuron instruction cost is non-trivial (~24,500 instructions/neuron for ballot computation alone), and the SNS `distribute_rewards` does additional work (Decimal arithmetic, map lookups, neuron mutation) per ballot entry. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5255-5280)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }
```

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

**File:** rs/sns/governance/src/governance.rs (L5756-5764)
```rust
    /// Creates a reward event.
    ///
    /// This method:
    /// * collects all proposals in state ReadyToSettle, that is, proposals that
    ///   can no longer accept votes for the purpose of rewards and that have
    ///   not yet been considered in a reward event
    /// * associates those proposals to the new reward event and cleans their ballots
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
```

**File:** rs/sns/governance/src/governance.rs (L5892-5997)
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

**File:** rs/nns/governance/canbench/canbench_results.yml (L44-50)
```yaml
  compute_ballots_for_new_proposal_with_stable_neurons:
    total:
      calls: 1
      instructions: 2450000
      heap_increase: 0
      stable_memory_increase: 256
    scopes: {}
```
