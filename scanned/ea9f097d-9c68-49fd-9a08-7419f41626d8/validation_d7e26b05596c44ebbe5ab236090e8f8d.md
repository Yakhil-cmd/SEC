### Title
Pending Voting Rewards Not Flushed Before `merge_neurons()` Leads to Incomplete Maturity Consolidation - (File: `rs/nns/governance/src/governance.rs`)

### Summary
The `merge_neurons()` function in NNS governance reads the source neuron's `maturity_e8s_equivalent` directly without first flushing pending rewards from the `RewardsDistributionStateMachine`. If a daily reward event has been calculated but not yet applied to neurons (i.e., the rewards are queued in the state machine but the timer task has not yet run), those pending rewards are excluded from the merge transfer and are instead applied to the source neuron after the merge completes — the opposite of the user's intent.

### Finding Description
The NNS governance reward pipeline is two-phase:

**Phase 1** — `distribute_voting_rewards_to_neurons()` calculates rewards and enqueues them into a `RewardsDistributionStateMachine` via `schedule_pending_rewards_distribution()`. At this point, neuron `maturity_e8s_equivalent` fields are **not yet updated**. [1](#0-0) [2](#0-1) 

**Phase 2** — A `DistributeRewardsTask` timer fires every 2 seconds and calls `distribute_pending_rewards()`, which iterates the queued map and increments each neuron's `maturity_e8s_equivalent`. [3](#0-2) [4](#0-3) 

`merge_neurons()` computes the maturity to transfer by reading the source neuron's current `maturity_e8s_equivalent` directly from the neuron store, with no step to flush the pending-rewards queue first: [5](#0-4) [6](#0-5) 

The `transfer_maturity_e8s` field is set to the source neuron's current (stale) `maturity_e8s_equivalent`. After the merge, the source neuron's maturity is zeroed and the target neuron receives only the already-applied portion: [7](#0-6) 

When the timer subsequently fires and processes the queued reward for the source neuron, it adds the pending amount to the source neuron's (now-zero) maturity: [8](#0-7) 

The result is that the source neuron ends up with residual maturity equal to the pending reward, directly contradicting the user's intent to consolidate all maturity into the target neuron.

### Impact Explanation
A user who calls `merge_neurons()` during the window between Phase 1 (reward calculation) and Phase 2 (reward application) will find that:
- The target neuron receives only the already-applied maturity from the source.
- The pending reward for the source neuron is applied to the source neuron **after** the merge, leaving residual maturity in the source neuron.
- The user must perform an additional operation (e.g., `disburse_maturity` or another merge) on the source neuron to recover the residual maturity.

No funds are permanently lost, but the merge is semantically incomplete and the user's consolidation intent is violated. This is a governance accounting correctness bug reachable by any unprivileged `manage_neuron` caller.

### Likelihood Explanation
The `DistributeRewardsTask` interval is 2 seconds, so the vulnerable window is narrow. However, reward events are calculated once per day, and the pending-rewards queue can hold multiple days' worth of distributions if the timer was delayed (e.g., due to instruction-limit throttling across many neurons). The `rounds_since_last_distribution` field in `RewardEvent` explicitly documents that missed rounds can accumulate: [9](#0-8) 

Any user who calls `merge_neurons()` immediately after a reward event — a natural time to consolidate neurons — is exposed to this race. Likelihood is **low** in normal operation but non-zero and deterministically reproducible.

### Recommendation
At the start of `merge_neurons()`, call `self.distribute_pending_rewards()` in a loop until it returns `false` (no work left), ensuring all queued rewards are applied to neurons before the merge reads their maturity values. This mirrors the recommendation in the original report: call the settlement function (`collect()` / `distribute_pending_rewards()`) before the operation that depends on settled state. [10](#0-9) 

### Proof of Concept
1. Advance governance time past one reward period so `distribute_voting_rewards_to_neurons()` is triggered, enqueuing rewards into `RewardsDistributionStateMachine` for source neuron S.
2. **Before** the 2-second timer fires (i.e., before `distribute_pending_rewards()` runs), call `manage_neuron` with `Command::Merge { source_neuron_id: S }` targeting neuron T.
3. Observe that `merge_neurons()` reads `S.maturity_e8s_equivalent = 0` (rewards not yet applied) and transfers 0 maturity to T.
4. Allow the timer to fire; observe that the pending reward is now applied to S (maturity > 0), while T received nothing from S's reward.
5. The user must separately disburse or re-merge S's residual maturity. [11](#0-10) [12](#0-11)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2429-2450)
```rust
    pub async fn merge_neurons(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        merge: &manage_neuron::Merge,
    ) -> Result<ManageNeuronResponse, GovernanceError> {
        let now = self.env.now();
        let in_flight_command = NeuronInFlightCommand {
            timestamp: now,
            command: Some(InFlightCommand::Merge(*merge)),
        };

        // Step 1: calculates the effect of the merge.
        let effect = calculate_merge_neurons_effect(
            id,
            merge,
            caller,
            &self.neuron_store,
            self.transaction_fee(),
            now,
        )?;

```

**File:** rs/nns/governance/src/governance.rs (L6797-6802)
```rust
        if let Some(reward_distribution) = reward_distribution {
            self.schedule_pending_rewards_distribution(
                new_reward_event.day_after_genesis,
                reward_distribution,
            );
        }
```

**File:** rs/nns/governance/src/reward/distribution.rs (L20-39)
```rust
    pub(crate) fn schedule_pending_rewards_distribution(
        &self,
        day_after_genesis: u64,
        distribution: RewardsDistribution,
    ) {
        let result =
            with_rewards_distribution_state_machine_mut(|rewards_distribution_state_machine| {
                rewards_distribution_state_machine
                    .add_rewards_distribution(day_after_genesis, distribution)
            });

        if let Err(e) = result {
            println!("{}Error scheduling rewards distribution: {}", LOG_PREFIX, e);
        }

        // TODO(NNS1-3643) Determine if there is a way we can refactor this so that
        // canbench can call timer setting function stubs (or even immediately execute the work)
        #[cfg(not(feature = "canbench-rs"))]
        run_distribute_rewards_periodic_task();
    }
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

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L17-40)
```rust
/// All possible effect of merging 2 neurons.
#[derive(Clone, Eq, PartialEq, Debug)]
pub struct MergeNeuronsEffect {
    /// The source neuron id.
    source_neuron_id: NeuronId,
    /// The target neuron id.
    target_neuron_id: NeuronId,
    /// The burning of neuron fees for the source neuron.
    source_burn_fees_e8s: Option<u64>,
    /// The stake transfer between the source and target neuron.
    stake_transfer_to_target_e8s: Option<u64>,
    /// The maturity to transfer from source to target.
    transfer_maturity_e8s: u64,
    /// The staked maturity to transfer from source to target.
    transfer_staked_maturity_e8s: u64,
    /// The bonus base to transfer from source to target.
    transfer_eight_year_gang_bonus_base_e8s: u64,
    /// The new dissolve state and age of the source neuron.
    source_neuron_dissolve_state_and_age: DissolveStateAndAge,
    /// The new dissolve state and age of the target neuron.
    target_neuron_dissolve_state_and_age: DissolveStateAndAge,
    /// The transaction fee as e8s.
    transaction_fees_e8s: u64,
}
```

**File:** rs/nns/governance/src/governance/merge_neurons.rs (L69-85)
```rust
    pub fn source_effect(&self) -> MergeNeuronsSourceEffect {
        MergeNeuronsSourceEffect {
            dissolve_state_and_age: self.source_neuron_dissolve_state_and_age,
            subtract_maturity: self.transfer_maturity_e8s,
            subtract_staked_maturity: self.transfer_staked_maturity_e8s,
            subtract_eight_year_gang_bonus_base_e8s: self.transfer_eight_year_gang_bonus_base_e8s,
        }
    }

    pub fn target_effect(&self) -> MergeNeuronsTargetEffect {
        MergeNeuronsTargetEffect {
            dissolve_state_and_age: self.target_neuron_dissolve_state_and_age,
            add_maturity: self.transfer_maturity_e8s,
            add_staked_maturity: self.transfer_staked_maturity_e8s,
            add_eight_year_gang_bonus_base_e8s: self.transfer_eight_year_gang_bonus_base_e8s,
        }
    }
```

**File:** rs/nns/governance/api/src/types.rs (L2286-2298)
```rust
    ///
    /// In normal operation, this field will almost always be 1. There are two
    /// reasons that rewards might not be distributed in a given round.
    ///
    /// 1. "Missed" rounds: there was a long period when we did calculate rewards
    ///     (longer than 1 round). (I.e. distribute_rewards was not called by
    ///     heartbeat for whatever reason, most likely some kind of bug.)
    ///
    /// 2. Rollover: We tried to distribute rewards, but there were no proposals
    ///     settled to distribute rewards for.
    ///
    /// In both of these cases, the rewards purse rolls over into the next round.
    pub rounds_since_last_distribution: Option<u64>,
```
