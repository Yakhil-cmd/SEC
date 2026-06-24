### Title
SNS Governance `distribute_rewards` Unbounded Loop Over Missed Reward Rounds Causes Permanent Instruction Limit Trap - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `distribute_rewards` function contains an unbounded `for` loop that iterates over every missed reward round since the last distribution. If the canister is dormant for a sufficiently long period (e.g., due to cycle exhaustion), `new_rounds_count` grows without bound. When the canister resumes, every invocation of `run_periodic_tasks` traps at the instruction limit inside this loop, permanently disabling reward distribution. The NNS governance explicitly fixed this class of bug (batched timer tasks with instruction-limit checks), but the SNS governance has not received the same fix.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the number of elapsed reward rounds and iterates over all of them in a single synchronous call:

```rust
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
``` [1](#0-0) 

```rust
for i in 1..=new_rounds_count {
    let seconds_since_genesis = ...;
    let current_reward_rate = voting_rewards_parameters.reward_rate_at(...);
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
``` [2](#0-1) 

There is no instruction-limit check inside this loop. `new_rounds_count` is unbounded — it equals `(now - last_reward_timestamp) / round_duration_seconds`. If the canister is dormant for a long time, this value grows proportionally.

After the loop, `distribute_rewards` also iterates over all settled proposals' ballots and all rewarded neurons without any instruction-limit guard:

```rust
for proposal_id in &considered_proposals {
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots { ... }
    }
}
``` [3](#0-2) 

```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron: &mut Neuron = match self.get_neuron_result_mut(&neuron_id) { ... };
    ...
}
``` [4](#0-3) 

`distribute_rewards` is called from `run_periodic_tasks`, which is triggered by a recurring timer: [5](#0-4) [6](#0-5) 

The NNS governance explicitly recognized and fixed this exact class of bug. Its CHANGELOG states:

> "Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages."
> "Unstaking maturity task has a limit of 100 neurons per message, which prevents it from exceeding instruction limit." [7](#0-6) 

The NNS fix uses `is_message_over_threshold` inside `continue_processing` to break the loop when the instruction budget is nearly exhausted:

```rust
if is_over_instructions_limit() {
    break;
}
``` [8](#0-7) 

The SNS governance has no equivalent protection.

---

### Impact Explanation

If `new_rounds_count` is large enough to exhaust the per-message instruction limit (5 billion instructions on the IC), every subsequent call to `run_periodic_tasks` traps before completing. Because the reward event timestamp is only updated at the end of a successful `distribute_rewards` call, the canister is permanently stuck: each retry starts from the same large `new_rounds_count` and traps again. This permanently disables:

- Voting reward distribution to all SNS neuron holders.
- Proposal settlement (ballots are cleared only after a successful reward event).
- Any downstream logic gated on reward events.

This constitutes a **cycles/resource accounting bug** causing a **permanent canister liveness failure** for a critical governance function.

---

### Likelihood Explanation

The trigger condition is SNS governance canister dormancy long enough to accumulate a large `new_rounds_count`. This is reachable without any privileged access:

1. **Cycle exhaustion**: Any SNS canister can run out of cycles if its community stops topping it up. After being topped up, the canister resumes with a large backlog.
2. **Small `round_duration_seconds`**: The SNS community can vote to set a short reward round duration. With `round_duration_seconds` at its minimum (e.g., 1 day), even a few years of dormancy produces ~1,000+ iterations. With a shorter minimum, the threshold is reached faster.
3. **No attacker required**: This is a self-inflicted liveness failure triggered by normal operational conditions (cycle exhaustion + recovery).

The `MAX_NUMBER_OF_NEURONS_CEILING` for SNS is 200,000 and `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING` is 700, meaning the ballot-iteration inner loops compound the instruction cost. [9](#0-8) 

---

### Recommendation

Apply the same batched-distribution pattern used by NNS governance:

1. After computing `new_rounds_count`, cap the number of rounds processed per call (e.g., process at most N rounds per invocation and reschedule via timer for the remainder).
2. Add an `is_message_over_threshold` guard inside the ballot-iteration and neuron-reward loops, breaking and rescheduling when the instruction budget is nearly exhausted.
3. Persist intermediate state (e.g., `rounds_processed_so_far`) so that successive timer invocations resume where the previous one left off, analogous to `RewardsDistributionStateMachine` in NNS. [10](#0-9) 

---

### Proof of Concept

1. Deploy an SNS with `round_duration_seconds` set to the minimum allowed value (e.g., 1 day).
2. Allow the SNS governance canister to run out of cycles so `run_periodic_tasks` stops firing.
3. Wait several years (or simulate by manipulating the canister's `latest_reward_event.end_timestamp_seconds` to a value far in the past).
4. Top up the canister with cycles.
5. Observe that the next `run_periodic_tasks` invocation traps with `Canister exceeded the instruction limit for single message execution`.
6. Observe that `latest_reward_event` is never updated, so every subsequent invocation also traps — the canister is permanently unable to distribute rewards.

The root cause is at: [2](#0-1) 

with no analog to the NNS fix at: [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5509-5514)
```rust
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
```

**File:** rs/sns/governance/src/governance.rs (L5812-5814)
```rust
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
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

**File:** rs/sns/governance/canister/canister.rs (L632-634)
```rust
    let new_timer_id = ic_cdk_timers::set_timer_interval(RUN_PERIODIC_TASKS_INTERVAL, async || {
        run_periodic_tasks().await
    });
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
