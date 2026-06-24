### Title
Unbounded Loops in SNS Governance `distribute_rewards` Cause Permanent Reward-Distribution DoS — (`File: rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `distribute_rewards` function contains multiple sequential unbounded loops over all neurons, all settled proposals, and all per-proposal ballots, with **zero instruction-limit checks**. As an SNS grows organically (more stakers, more proposals), a single periodic-task invocation will eventually exhaust the IC's hard instruction limit, causing the message to trap and roll back. Because the state never advances, every subsequent timer invocation traps identically, permanently breaking reward distribution for all SNS participants. The NNS governance canister suffered the same class of bug and was explicitly patched with a batched, instruction-aware distribution mechanism; the SNS canister has not received the equivalent fix.

---

### Finding Description

`distribute_rewards` is called from `run_periodic_tasks` whenever `should_distribute_rewards()` returns `true`. [1](#0-0) 

Inside `distribute_rewards`, three sequential loops run with no instruction-limit guard:

**Loop 1 — reward-round accumulation** (`new_rounds_count` iterations, unbounded if rewards were missed for many rounds): [2](#0-1) 

**Loop 2 — nested proposal × ballot scan** (up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS × neuron_count` iterations; each inner iteration calls `NeuronId::from_str` and mutates a `HashMap`): [3](#0-2) 

**Loop 3 — per-neuron maturity increment** (one iteration per voting neuron, no break): [4](#0-3) 

A grep for `is_message_over_threshold`, `instruction_counter`, or any equivalent guard in `rs/sns/governance/src/` returns **zero matches**, confirming no instruction-limit protection exists anywhere in the SNS governance source.

The NNS governance canister was explicitly fixed for the identical pattern. Its `continue_processing` breaks after each neuron reward if the instruction budget is exhausted: [5](#0-4) 

The NNS CHANGELOG documents the rationale: [6](#0-5) 

A secondary unbounded loop exists in `compute_ballots_for_new_proposal`, which iterates over every neuron in `self.proto.neurons` on every proposal submission with no instruction guard: [7](#0-6) 

---

### Impact Explanation

When the combined work in `distribute_rewards` exceeds the IC's hard instruction limit (~40 billion instructions with DTS), the canister message traps and all state changes are rolled back. Because the neuron/proposal state that caused the trap is never modified by the failed call, every subsequent timer invocation of `run_periodic_tasks` will attempt the same computation, trap again, and roll back again. The result is:

- **Permanent loss of voting-reward distribution** for all SNS token holders.
- **Periodic-task stall**: `run_periodic_tasks` is a single async function; a trap inside it prevents the remaining tasks (upgrade checks, maturity moves, GC) from completing in the same invocation.
- **Canister upgrade risk**: if the canister cannot complete a heartbeat/timer without trapping, upgrade proposals that depend on periodic-task health checks may also be affected.

---

### Likelihood Explanation

The trigger is purely organic growth, requiring no adversarial action:

1. **Neuron count**: Any user holding SNS tokens can stake and create a neuron. Popular SNS DAOs already have thousands of neurons. Each neuron adds one entry to Loop 3 and one ballot entry to Loop 2 per proposal.
2. **Proposal count**: Any neuron holder meeting the minimum dissolve-delay and stake requirements can submit proposals up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS`. Each open proposal multiplies the ballot scan in Loop 2.
3. **Missed rounds**: If the canister is upgraded or the timer misfires, `new_rounds_count` in Loop 1 grows proportionally to the gap, adding further unbounded work.

An adversary can accelerate the condition by creating many neurons (staking is permissionless) and submitting proposals up to the cap, but the vulnerability manifests without any adversarial intent in a sufficiently active SNS.

---

### Recommendation

Apply the same batched-distribution pattern already used in NNS governance:

1. **Split `distribute_rewards` into a calculation phase and a distribution phase.** Store the per-neuron reward map in stable memory (analogous to `RewardsDistributionInProgress` in NNS).
2. **Add an instruction-limit guard** inside each loop body, identical to the NNS `continue_processing` pattern:
   ```rust
   if is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT) {
       break;
   }
   ```
3. **Re-schedule a timer** to continue distribution in the next message if work remains, mirroring `run_distribute_rewards_periodic_task` in NNS. [8](#0-7) 

4. **Cap `new_rounds_count`** in Loop 1 to a safe maximum per invocation (e.g., 100 rounds), carrying the remainder forward.
5. Apply the same instruction-limit guard to `compute_ballots_for_new_proposal` in SNS governance.

---

### Proof of Concept

**Setup**: Deploy an SNS with `round_duration_seconds = 86400` (1 day). Have 10,000 users each stake tokens and create a neuron. Submit 100 proposals (the maximum with ballots). Allow all proposals to reach `ReadyToSettle`.

**Trigger**: Wait for the next `run_periodic_tasks` timer invocation. `should_distribute_rewards()` returns `true`.

**Execution path**:
- `run_periodic_tasks` → `distribute_rewards(supply)` at line 5513.
- Loop 2 executes `100 proposals × 10,000 ballots = 1,000,000` iterations, each calling `NeuronId::from_str(voter)` (string parsing) and mutating a `HashMap`.
- Loop 3 executes `10,000` iterations, each calling `get_neuron_result_mut`.
- Total instruction consumption exceeds the IC hard limit; the message traps.
- State rolls back; `latest_reward_event` is unchanged.
- Next timer fires, same computation, same trap — permanently.

**Observable effect**: `get_latest_reward_event` returns a stale event indefinitely; no neuron's `maturity_e8s_equivalent` ever increases; SNS voting rewards are permanently frozen. [9](#0-8) [10](#0-9) [11](#0-10) [4](#0-3)

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

**File:** rs/sns/governance/src/governance.rs (L5763-5765)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
        let now = self.env.now();
```

**File:** rs/sns/governance/src/governance.rs (L5861-5875)
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

            result
        };
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

**File:** rs/nns/governance/src/timer_tasks/distribute_rewards.rs (L43-55)
```rust
impl PeriodicSyncTask for DistributeRewardsTask {
    fn execute(self) {
        self.governance.with_borrow_mut(|governance| {
            let work_left = governance.distribute_pending_rewards();
            if !work_left {
                cancel_distribute_pending_rewards_timer();
            }
        });
    }

    const NAME: &'static str = "distribute_rewards";
    const INTERVAL: Duration = Duration::from_secs(2);
}
```
