### Title
Pending Voting Rewards Silently Dropped for Neurons Disbursed Between Async Calculation and Distribution Phases - (`rs/nns/governance/src/reward/distribution.rs`)

---

### Summary

The NNS governance canister introduced a two-phase asynchronous reward distribution system. In Phase 1, rewards are calculated and neuron existence is verified. In Phase 2, a timer task delivers the maturity to each neuron. If a neuron is disbursed (removed from the neuron store) between Phase 1 and Phase 2, the reward entry is silently dropped with no rollback, no retry, and no rollover — permanently losing the maturity that was already recorded as distributed in the `RewardEvent`.

---

### Finding Description

**Phase 1 — `calculate_voting_rewards` in `rs/nns/governance/src/governance.rs`:**

At reward calculation time, the code checks whether each voting neuron still exists and, if so, adds it to a `RewardsDistribution` map and increments `actually_distributed_e8s_equivalent`: [1](#0-0) 

The `RewardEvent` is then committed with `distributed_e8s_equivalent` reflecting the reward for neuron N, and the distribution is persisted to stable memory via `schedule_pending_rewards_distribution`: [2](#0-1) 

**Phase 2 — `continue_processing` in `rs/nns/governance/src/reward/distribution.rs`:**

A periodic timer task (firing every 2 seconds) calls `distribute_pending_rewards`, which calls `continue_processing`. This function uses `pop_first()` to remove each entry from the map **before** attempting to write maturity to the neuron. If the neuron is not found, the error is logged and execution continues — the entry is already gone and the reward is permanently lost: [3](#0-2) 

The comment "This should not be possible as neuron existence is checked when rewards are calculated" reflects the developers' assumption that no neuron can disappear between Phase 1 and Phase 2. This assumption is violated because `disburse` can remove a neuron from the store in a separate ingress message that executes between the two timer firings.

**The `RewardsDistributionInProgress` proto** is stored in stable memory and survives upgrades, meaning the pending reward for a disbursed neuron persists until the timer processes it and silently drops it: [4](#0-3) 

---

### Impact Explanation

The `RewardEvent.distributed_e8s_equivalent` records the reward as having been distributed, but the maturity is never credited to the neuron. This is a **ledger conservation bug**: the governance canister's accounting claims more maturity was distributed than was actually delivered. The neuron controller loses voting rewards they legitimately earned. The lost amount equals the neuron's proportional share of the daily reward pool, which can be substantial for large neurons. The rewards are not redirected — they are simply destroyed.

---

### Likelihood Explanation

The race window is the 2-second timer interval (`DistributeRewardsTask::INTERVAL`). A neuron controller who calls `disburse` in this window after a reward round closes will lose their rewards. This is not a targeted attack but a self-inflicted loss that can occur during normal operation. With hundreds of thousands of neurons and daily reward rounds, the probability of at least one neuron being disbursed in this window over time is non-negligible. The CHANGELOG confirms this two-phase async system was recently introduced (Proposal 135702, 2025-03-08), making it a new attack surface: [5](#0-4) 

---

### Recommendation

In `continue_processing`, when `neuron_store.with_neuron_mut` returns an error, re-insert the `(id, reward_e8s)` entry back into `self.rewards` rather than silently dropping it. Alternatively, roll the lost amount into the next reward event's `e8s_equivalent_to_be_rolled_over` so the maturity is not destroyed. A guard should also be added to prevent neuron disbursement while a `RewardsDistributionInProgress` entry exists for that neuron ID. [6](#0-5) 

---

### Proof of Concept

1. Reward round closes. `calculate_voting_rewards` runs. Neuron N (large stake, voted Yes) exists → added to `RewardsDistribution` with reward R e8s. `distributed_e8s_equivalent` is incremented by R. `schedule_pending_rewards_distribution` persists the distribution to stable memory. `latest_reward_event` is updated.

2. In the same or next ingress message (before the 2-second timer fires), neuron N's controller calls `disburse`. The neuron is removed from the neuron store.

3. The `DistributeRewardsTask` timer fires. `distribute_pending_rewards` → `continue_processing` calls `self.rewards.pop_first()`, removing neuron N's entry. `neuron_store.with_neuron_mut(&id, ...)` returns `Err` (neuron not found). The error is logged. The loop continues. Neuron N's R e8s of maturity is permanently lost.

4. The `RewardEvent` records `distributed_e8s_equivalent` as if R e8s were delivered, but they were not. The governance canister's accounting is permanently inconsistent.

This is directly analogous to the Blend M-16 finding: in both cases, rewards are recorded as distributed before the recipient can claim them, and a state change (pool removal / neuron disbursal) that occurs in the gap between distribution recording and actual delivery causes permanent, irrecoverable loss of the recorded rewards. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6722-6744)
```rust
            for (neuron_id, used_voting_rights) in voters_to_used_voting_right {
                if self.neuron_store.contains(neuron_id) {
                    let reward = (used_voting_rights * total_available_e8s_equivalent_float
                        / total_voting_rights) as u64;

                    reward_distribution.add_reward(neuron_id, reward);

                    // NOTE: This is the only reason we are checking the existence of neurons
                    // at this stage. Otherwise, we could defer until we distribute them in the
                    // schedule task.
                    actually_distributed_e8s_equivalent += reward;
                } else {
                    println!(
                        "{}Cannot find neuron {}, despite having voted with power {} \
                            in the considered reward period. The reward that should have been \
                            distributed to this neuron is simply skipped, so the total amount \
                            of distributed reward for this period will be lower than the maximum \
                            allowed.",
                        LOG_PREFIX, neuron_id.id, used_voting_rights
                    );
                }
            }
            Some(reward_distribution)
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

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2767-2772)
```text
// A reward disbribution that has been calculated but not fully disbursed.
// This supports large reward distributions that may need to be split into multiple
// messages.
message RewardsDistributionInProgress {
  map<uint64, uint64> neuron_ids_to_e8_amounts = 1;
}
```

**File:** rs/nns/governance/CHANGELOG.md (L666-671)
```markdown
* Voting Rewards will be scheduled by a timer instead of by heartbeats.
* Unstaking maturity task will be processing up to 100 neurons in a single message, to avoid
  exceeding the instruction limit in a single execution.
* Voting Rewards will be distributed asynchronously in the background after being calculated.
    * This will allow rewards to be compatible with neurons being stored in Stable Memory.
* Ramp up the failure rate of _pb method to 0.7 again.
```
