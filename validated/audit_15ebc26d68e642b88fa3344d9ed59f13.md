### Title
Unbounded Reward-Distribution Loop in SNS Governance Can Permanently Exhaust Instruction Limit — (`rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance canister's `distribute_rewards` function contains multiple unbounded loops over reward rounds, proposal ballots, and voting neurons with no instruction-limit guard. The NNS governance canister has already been patched with a `DISTRIBUTION_MESSAGE_LIMIT` / `is_message_over_threshold` mechanism that breaks the equivalent loop across multiple timer messages. The SNS canister has not received this fix. If the instruction limit is exhausted mid-execution, the entire `run_periodic_tasks` message traps, all state changes are rolled back, `latest_reward_event` is never updated, and every subsequent invocation re-enters the same oversized loop — permanently halting SNS reward distribution.

### Finding Description

`distribute_rewards` in `rs/sns/governance/src/governance.rs` contains three nested, unbounded loops:

**Loop 1 — reward-round accumulation** (line 5861):
```rust
for i in 1..=new_rounds_count {
    let current_reward_rate = voting_rewards_parameters.reward_rate_at(...);
    result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
}
```
`new_rounds_count` is computed as `(now − reward_start) / round_duration_seconds`. [1](#0-0) 

If `round_duration_seconds` is set to a small value (the proto only rejects `0`; `1` is accepted), `new_rounds_count` grows proportionally to elapsed time. After one year at `round_duration_seconds = 1`, `new_rounds_count ≈ 31.5 million`. [2](#0-1) 

**Loop 2 — ballot aggregation** (lines 5894–5930):
```rust
for proposal_id in &considered_proposals {
    for (voter, ballot) in &proposal.ballots { ... }
}
```
Iterates over every ballot of every settled proposal with no bound check. [3](#0-2) 

**Loop 3 — neuron maturity update** (line 5954):
```rust
for (neuron_id, neuron_reward_shares) in neuron_id_to_reward_shares {
    let neuron = self.get_neuron_result_mut(&neuron_id)?;
    neuron.maturity_e8s_equivalent += neuron_reward_e8s;
}
```
Iterates over every neuron that voted, with no instruction-limit check. [4](#0-3) 

None of these loops contain any call to `is_message_over_threshold` or any equivalent guard.

**Contrast with the NNS fix.** The NNS governance canister was explicitly patched (see CHANGELOG entry: *"Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages"*). The fix introduces `DISTRIBUTION_MESSAGE_LIMIT = 1_000_000_000` instructions and breaks the neuron-update loop across multiple 2-second timer firings: [5](#0-4) [6](#0-5) [7](#0-6) 

The SNS governance canister has no equivalent mechanism. [8](#0-7) 

`distribute_rewards` is called synchronously inside `run_periodic_tasks`, which is itself invoked by a timer callback: [9](#0-8) 

Because the call is synchronous and there is no DTS (Deterministic Time Slicing) for timer callbacks in the same way as for update messages, exhausting the per-message instruction limit causes the entire message to trap and all state to roll back.

### Impact Explanation

When the instruction limit is exceeded inside `distribute_rewards`:

1. The IC runtime traps the message; all mutations (including the `latest_reward_event` update at the end of the function) are rolled back.
2. On the next timer firing, `should_distribute_rewards()` returns `true` again because `latest_reward_event` was never advanced.
3. `distribute_rewards` is called again with the same (or larger) `new_rounds_count`, exhausting instructions again.
4. This creates a permanent, self-reinforcing DoS: **SNS voting rewards are never distributed again**.

Neurons stop accumulating maturity, breaking the economic incentive model of the SNS. The SNS governance canister continues to function for other operations (proposals, voting) but reward distribution is permanently halted with no on-chain recovery path short of an upgrade that patches the loop.

### Likelihood Explanation

Two realistic trigger conditions exist:

1. **Small `round_duration_seconds`**: The proto validation only rejects `0`; a value of `1` second is accepted. An SNS launched with or migrated to `round_duration_seconds = 1` will hit the limit within months. This can be set at genesis or via a governance proposal that passes with a simple majority of voting power — reachable by any sufficiently large neuron holder coalition, not requiring a privileged key.

2. **Large neuron count + many settled proposals**: An SNS with `max_number_of_neurons` set high and many active voters across many proposals accumulates a large `neuron_id_to_reward_shares` map. The per-neuron work in loop 3 (stable-memory lookup + arithmetic) is non-trivial; with tens of thousands of neurons this loop alone can exhaust the 5-billion-instruction budget.

Both conditions are reachable by unprivileged SNS participants (token holders who create neurons and vote) without any threshold-majority corruption.

### Recommendation

Apply the same fix used in NNS governance:

1. Move the neuron-maturity update (loop 3) into a persistent state machine stored in stable memory, processed in batches across multiple timer messages.
2. Add an `is_message_over_threshold` guard inside the reward-round accumulation loop (loop 1) or cap `new_rounds_count` to a safe maximum per invocation, carrying over the remainder to the next timer firing.
3. Add a validation lower bound on `round_duration_seconds` (e.g., ≥ 3600 seconds) to prevent pathologically small values from being set via governance proposal.

### Proof of Concept

**Trigger via small `round_duration_seconds`:**

1. Deploy an SNS with `VotingRewardsParameters { round_duration_seconds: Some(1), ... }`.
2. Wait 60 days. `new_rounds_count = 60 × 86400 = 5,184,000`.
3. Observe that the next `run_periodic_tasks` timer fires and calls `distribute_rewards`.
4. Loop 1 executes 5.18 million iterations of `reward_rate_at(...)` (floating-point arithmetic + Decimal operations). At ~1000 instructions per iteration this is ~5.18 billion instructions — exceeding the 5-billion per-message limit.
5. The message traps; `latest_reward_event` is not updated.
6. Every subsequent timer firing repeats steps 3–5 with an ever-growing `new_rounds_count`.
7. No rewards are ever distributed; the SNS reward system is permanently halted.

**Relevant code path:**

`run_periodic_tasks` → `distribute_rewards` → `for i in 1..=new_rounds_count` (line 5861) → instruction limit exceeded → trap → rollback → repeat. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5471-5521)
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
```

**File:** rs/sns/governance/src/governance.rs (L5796-5806)
```rust
        if round_duration_seconds == 0 {
            // This is important, but emitting this every time will be spammy, because this gets
            // called during run_periodic_tasks.
            log!(
                ERROR,
                "round_duration_seconds ({}) is not positive. \
                 Therefore, we cannot calculate voting rewards.",
                round_duration_seconds,
            );
            return;
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

**File:** rs/nns/governance/src/reward/distribution.rs (L17-17)
```rust
const DISTRIBUTION_MESSAGE_LIMIT: u64 = BILLION;
```

**File:** rs/nns/governance/src/reward/distribution.rs (L43-47)
```rust
        let is_over_instructions_limit = || is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT);
        with_rewards_distribution_state_machine_mut(|rewards_distribution_state_machine| {
            rewards_distribution_state_machine.with_next_distribution(|(_, distribution)| {
                distribution
                    .continue_processing(&mut self.neuron_store, is_over_instructions_limit);
```

**File:** rs/nns/governance/src/reward/distribution.rs (L184-186)
```rust
            if is_over_instructions_limit() {
                break;
            }
```

**File:** rs/sns/governance/canister/canister.rs (L605-611)
```rust
async fn run_periodic_tasks() {
    if let Some(ref mut timers) = governance_mut().proto.timers {
        timers.last_spawned_timestamp_seconds.replace(now_seconds());
    };

    governance_mut().run_periodic_tasks().await;
}
```
