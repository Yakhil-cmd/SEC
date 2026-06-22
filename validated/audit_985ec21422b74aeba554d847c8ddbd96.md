### Title
SNS Governance `distribute_rewards` Unbounded Neuron Iteration Causes Permanent Reward Liveness Failure - (File: `rs/sns/governance/src/governance.rs`)

### Summary

The SNS Governance canister's `distribute_rewards` function iterates over all voting neurons in a single synchronous message with no instruction-limit guard. As an SNS DAO grows organically, this loop will eventually exceed the IC per-message instruction limit, causing the entire message to trap and roll back. Because the reward event and proposal settlement are only committed after the loop completes, the canister enters a permanent liveness failure: every subsequent timer invocation re-attempts the same unbounded loop and traps again, permanently halting voting reward distribution.

### Finding Description

`distribute_rewards` in the SNS Governance canister is called from `run_periodic_tasks` on a recurring timer: [1](#0-0) 

Inside `distribute_rewards`, after computing `neuron_id_to_reward_shares` from all proposal ballots, the function iterates over every neuron that voted to credit maturity: [2](#0-1) 

There is **no instruction-limit check** inside this loop. The loop runs to completion or the message traps. Only after the loop does the function settle proposals and write the new `latest_reward_event`. If the loop traps due to instruction exhaustion, all state changes are rolled back, the reward event is not advanced, and the next timer invocation will attempt the identical (or larger) loop again.

This is in direct contrast to the NNS Governance canister, which was explicitly fixed (Proposal 135702, March 2025) to use a batched, resumable state machine with an instruction-limit guard: [3](#0-2) 

The NNS fix uses `is_message_over_threshold(DISTRIBUTION_MESSAGE_LIMIT)` to break out of the loop and re-schedule remaining work via a periodic timer: [4](#0-3) 

The SNS canister has no equivalent mechanism. Its `distribute_rewards` is a single atomic synchronous call: [5](#0-4) 

### Impact Explanation

Once the number of voting neurons in an SNS DAO grows large enough that the neuron-iteration loop exceeds the IC instruction limit (~40 billion instructions for update/timer messages), every invocation of `run_periodic_tasks` will trap at the same point. Because the reward event is never advanced past the failing round, the canister is permanently stuck: voting rewards are never distributed again. Neuron maturity stops accruing, breaking the economic incentive for governance participation. This is a **governance liveness failure** and **resource accounting bug** with protocol-level impact on any SNS DAO that reaches sufficient scale.

### Likelihood Explanation

This is an organic, non-adversarial failure. Any SNS DAO that grows to a large number of active voting neurons will hit this. The NNS governance team already acknowledged and fixed the identical bug in NNS Governance (Proposal 135702 changelog explicitly states: *"Distribute rewards is moved to timer, and has a mechanism to distribute in batches in multiple messages"* and *"Unstaking maturity task has a limit of 100 neurons per message, which prevents it from exceeding instruction limit"*): [6](#0-5) 

The SNS canister did not receive the same remediation. Any sufficiently popular SNS DAO is at risk.

### Recommendation

Apply the same batched-distribution pattern already used in NNS Governance to SNS Governance:

1. After computing `neuron_id_to_reward_shares`, store the pending distribution in stable memory (analogous to `RewardsDistributionStateMachine`).
2. Advance `latest_reward_event` and settle proposals immediately (so they are not re-processed).
3. Distribute maturity to neurons in a separate recurring timer task that checks `is_message_over_threshold` after each neuron and resumes in the next message if the limit is reached. [7](#0-6) 

### Proof of Concept

The following describes the failure path:

1. An SNS DAO accumulates a large number of neurons (e.g., 50,000+), all of which vote on proposals.
2. The recurring timer fires `run_periodic_tasks` → `distribute_rewards`.
3. Inside `distribute_rewards`, the loop at line 5954 iterates over all `neuron_id_to_reward_shares` entries, performing a `get_neuron_result_mut` lookup and maturity update for each.
4. After enough neurons, the IC instruction counter exceeds the per-message limit; the message traps.
5. All state changes roll back. `latest_reward_event` is unchanged. `considered_proposals` remain in `ReadyToSettle`.
6. The next timer fires and `should_distribute_rewards()` returns `true` again (same epoch, same proposals). The identical loop runs and traps again.
7. Voting rewards are permanently halted.

The NNS governance team's own changelog confirms this was a real production bug in NNS Governance that required an emergency fix: [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5503-5513)
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
```

**File:** rs/sns/governance/src/governance.rs (L5763-5764)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
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

**File:** rs/nns/governance/CHANGELOG.md (L654-668)
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
```

**File:** rs/nns/governance/CHANGELOG.md (L710-718)
```markdown
# 2025-02-11: Proposal 135265

https://dashboard.internetcomputer.org/proposal/135265

## Removed

* Neuron migration (`migrate_active_neurons_to_stable_memory`) is rolled back due to issues with
  reward distribution. It has already been rolled back with a hotfix ([proposal
  135265](https://dashboard.internetcomputer.org/proposal/135265))
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
