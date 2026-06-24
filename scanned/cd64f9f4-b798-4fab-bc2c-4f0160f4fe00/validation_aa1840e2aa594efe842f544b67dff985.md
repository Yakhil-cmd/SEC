### Title
Pending Voting Rewards Not Applied Before Maturity Disbursement Causes Users to Receive Less Than Entitled Amount - (File: `rs/nns/governance/src/governance/disburse_maturity.rs`)

### Summary

The NNS governance canister uses a two-phase, asynchronous reward distribution mechanism. Voting rewards are first calculated and scheduled into a `RewardsDistributionStateMachine`, then applied to neuron `maturity_e8s_equivalent` fields in a separate timer-driven task. When a neuron controller calls `DisburseMaturity` (including with `percentage_to_disburse: 100`) during the window between reward scheduling and reward application, the disbursement amount is computed against a stale, lower maturity balance. The pending rewards are eventually credited to the neuron but are excluded from the in-flight disbursement, causing the user to receive less than their full entitled maturity in that disbursement — a direct analog to the Sperax `rebase()`-after-burn ordering bug.

### Finding Description

The NNS governance reward pipeline has two distinct phases:

**Phase 1 — Reward Calculation & Scheduling** (`distribute_voting_rewards_to_neurons`): [1](#0-0) 

This calls `schedule_pending_rewards_distribution`, which stores the per-neuron reward amounts in a stable `RewardsDistributionStateMachine` BTreeMap but does **not** yet update any neuron's `maturity_e8s_equivalent`. [2](#0-1) 

**Phase 2 — Asynchronous Reward Application** (`distribute_pending_rewards` / `continue_processing`):

A periodic timer task (interval: 2 seconds) calls `distribute_pending_rewards()`, which pops entries from the state machine and increments each neuron's `maturity_e8s_equivalent` (or `staked_maturity_e8s_equivalent`): [3](#0-2) [4](#0-3) 

For large neuron populations, Phase 2 spans **multiple** timer invocations due to the instruction-limit check: [5](#0-4) 

**The vulnerable operation — `initiate_maturity_disbursement`:**

When a user calls `DisburseMaturity`, `initiate_maturity_disbursement` reads the neuron's current `maturity_e8s_equivalent` to compute the disbursement amount: [6](#0-5) 

It then locks in that amount and subtracts it from the neuron's maturity: [7](#0-6) 

There is no mechanism to flush or await pending rewards before reading `maturity_e8s_equivalent`. If Phase 2 has not yet completed for the current reward round, the value read is stale — it excludes rewards that have been calculated and scheduled but not yet applied.

### Impact Explanation

A neuron controller who calls `DisburseMaturity` with `percentage_to_disburse: 100` during the Phase 1→Phase 2 window will initiate a disbursement for less than their full entitled maturity. The pending rewards will eventually be credited to `maturity_e8s_equivalent` by the timer, but they are excluded from the already-locked disbursement record. The user must initiate a second disbursement (with another 7-day delay) to recover the missed rewards. This is a **governance/ledger conservation ordering bug**: the "rebase" (reward application) occurs after the "redeem" (maturity disbursement initiation) rather than before, causing users to receive less than their entitled yield in a single disbursement cycle.

### Likelihood Explanation

The vulnerability window exists every reward distribution period (daily). Phase 2 is explicitly designed to span multiple 2-second timer ticks for large neuron sets. Any `DisburseMaturity` call submitted between the heartbeat that triggers Phase 1 and the final timer tick that completes Phase 2 will be affected. This is reachable by any unprivileged neuron controller via a standard `manage_neuron` ingress call — no special privileges are required. The window is narrow (seconds to minutes depending on neuron count) but recurs daily and is predictable.

### Recommendation

Before computing `disbursement_maturity_e8s` in `initiate_maturity_disbursement`, check whether there are pending rewards for the neuron in the `RewardsDistributionStateMachine` and apply them first (or include them in the disbursement calculation). Alternatively, document clearly that users should wait for pending reward distribution to complete before calling `DisburseMaturity` with `percentage_to_disburse: 100`, analogous to the Sperax team's acknowledged mitigation of calling `rebase()` before `redeem()`.

### Proof of Concept

1. Reward round ends; `distribute_voting_rewards_to_neurons` runs on heartbeat, scheduling 1,000 ICP of maturity for neuron N via `schedule_pending_rewards_distribution`. Neuron N's `maturity_e8s_equivalent` is still 5,000 ICP (Phase 2 not yet run). [8](#0-7) 

2. Before the 2-second timer fires, neuron N's controller submits `DisburseMaturity { percentage_to_disburse: 100 }`.

3. `initiate_maturity_disbursement` reads `maturity_e8s_equivalent = 5,000 ICP` and locks in a disbursement of 5,000 ICP, subtracting it from the neuron's maturity. [9](#0-8) 

4. The timer fires; `continue_processing` adds 1,000 ICP to `maturity_e8s_equivalent`, which is now 0 + 1,000 = 1,000 ICP remaining in the neuron. [10](#0-9) 

5. After the 7-day delay, the user receives only 5,000 ICP instead of 6,000 ICP. The 1,000 ICP remains in the neuron and requires a second disbursement with another 7-day wait.

### Citations

**File:** rs/nns/governance/src/governance.rs (L6778-6802)
```rust
        let voting_rewards_calculation_result = self.calculate_voting_rewards(supply);
        let Some((new_reward_event, reward_distribution)) = voting_rewards_calculation_result
        else {
            return;
        };

        // Now the mutations begin. Once any mutation has happened, we cannot exit early without the
        // rest. Otherwise we could end up in an inconsistent state and break some properties we
        // would like to hold.
        //
        // The properties we would like to hold are:
        // * The rewards for a given day is only distributed once. This is made sure by updating the
        //   reward event, in particular the `day_after_genesis` field every time
        //   `schedule_pending_rewards_distribution` is called. This is also the main reason that
        //   once mutations begin, we should not exit early before  `latest_reward_event` is
        //   updated.
        // * The proposals should only be settled once. This is made sure by updating the proposal
        //   status from `ReadyToSettle` to `Settled`.

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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L276-292)
```rust
    ) = neuron_store
        .with_neuron(id, |neuron| {
            let is_neuron_spawning = neuron.state(now_seconds) == NeuronState::Spawning;
            let is_neuron_controlled_by_caller = neuron.is_controlled_by(caller);
            let num_disbursements = neuron.maturity_disbursements_in_progress().len();
            let maturity_e8s_equivalent = neuron.maturity_e8s_equivalent;
            (
                is_neuron_spawning,
                is_neuron_controlled_by_caller,
                num_disbursements,
                maturity_e8s_equivalent,
            )
        })
        .map_err(|_| InitiateMaturityDisbursementError::NeuronNotFound)?;

    let disbursement_maturity_e8s =
        percentage_of_maturity(maturity_e8s_equivalent, *percentage_to_disburse)?;
```

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L317-326)
```rust
    neuron_store
        .with_neuron_mut(id, |neuron| {
            neuron.add_maturity_disbursement_in_progress(disbursement_in_progress);
            neuron.maturity_e8s_equivalent = neuron
                .maturity_e8s_equivalent
                .saturating_sub(disbursement_maturity_e8s);
        })
        .map_err(|_| InitiateMaturityDisbursementError::Unknown {
            reason: "Failed to update neuron even though it was found before".to_string(),
        })?;
```
