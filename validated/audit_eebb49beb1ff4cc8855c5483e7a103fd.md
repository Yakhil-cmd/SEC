### Title
Voting Reward Maturity Permanently Lost When Neuron Is Dissolved Before Distribution - (File: rs/nns/governance/src/governance.rs)

### Summary
In both NNS and SNS governance, when a neuron votes on a proposal and is subsequently dissolved and disbursed before the reward distribution period executes, its proportional share of the reward purse is permanently destroyed rather than rolled over to the next period. The `total_available_e8s_equivalent` is fully consumed (not rolled over), but `distributed_e8s_equivalent` is lower. The gap is irrecoverably lost — an exact analog to the Voter.sol `claimable[_gauge]` lock-up.

### Finding Description

**NNS — `calculate_voting_rewards()` in `rs/nns/governance/src/governance.rs`:**

When iterating over voters to build the reward distribution, if a neuron voted but is no longer present in the neuron store, its share is silently skipped:

```rust
if self.neuron_store.contains(neuron_id) {
    let reward = (used_voting_rights * total_available_e8s_equivalent_float
        / total_voting_rights) as u64;
    reward_distribution.add_reward(neuron_id, reward);
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
``` [1](#0-0) 

The rollover logic only activates when `settled_proposals.is_empty()`:

```rust
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent
    } else {
        0  // ← returns 0 whenever any proposals were settled
    }
}
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
``` [2](#0-1) 

When proposals ARE settled (the normal distribution case), the entire `total_available_e8s_equivalent` is consumed from the rollover pool, but only `distributed_e8s_equivalent` is actually credited to neurons. The gap — the share belonging to dissolved neurons — is permanently destroyed.

**NNS — asynchronous `continue_processing()` in `rs/nns/governance/src/reward/distribution.rs`:**

A second loss path exists in the asynchronous distribution task. If a neuron is present in `RewardsDistributionInProgress` but is removed from the neuron store between scheduling and processing, the reward is silently dropped:

```rust
Err(e) => {
    println!(
        "{}Error rewarding neuron {:?} during reward_distribution.\
    This should not be possible as neuron existence is checked when \
    rewards are calculated: {}",
        LOG_PREFIX, id, e
    );
}
``` [3](#0-2) 

**SNS — `distribute_rewards()` in `rs/sns/governance/src/governance.rs`:**

The identical pattern exists in SNS governance:

```rust
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
            ...
        );
        continue;
    }
};
``` [4](#0-3) 

### Impact Explanation

Voting rewards (maturity equivalents) that were allocated to a neuron that no longer exists at distribution time are permanently destroyed. They are not redistributed to other voters, not rolled over to the next reward period, and not returned to any treasury. The total effective reward supply is permanently reduced by the missing share. This is a ledger conservation bug: the reward purse is fully consumed by the accounting logic, but a portion is never credited to anyone.

The NNS test suite explicitly confirms and accepts this behavior:

```
// We should have distributed 100 e8 equivalent if all voters still existed.
// Since neuron 999 is gone and had a voting power 3x that of neuron 2,
// only 1/4 is actually distributed.
distributed_e8s_equivalent: 25,
total_available_e8s_equivalent: 100,
``` [5](#0-4) 

The 75 e8s are permanently lost — not rolled over to the next event.

### Likelihood Explanation

Any unprivileged neuron owner can trigger this condition without any special access:

1. Vote on a proposal (or follow a neuron that votes).
2. Wait for the proposal to reach `ReadyToSettle` state.
3. Dissolve and disburse the neuron — a standard, permissionless operation — before the daily reward distribution timer fires.

The window is up to one full reward period (one day in NNS, configurable in SNS). The operation is entirely normal user behavior. No governance majority, no admin key, and no privileged access is required. The larger the dissolved neuron's voting power relative to the total, the larger the permanent loss.

### Recommendation

When a neuron's reward share cannot be distributed because the neuron no longer exists, the undistributed amount should be rolled over to the next reward period rather than being permanently destroyed. Concretely:

- In `calculate_voting_rewards()` (NNS) and `distribute_rewards()` (SNS), accumulate the skipped reward amounts into a `skipped_e8s` counter.
- Subtract `skipped_e8s` from `actually_distributed_e8s_equivalent` and add it back into the rollover pool for the next `RewardEvent`.
- Alternatively, update `e8s_equivalent_to_be_rolled_over()` to return `total_available_e8s_equivalent - distributed_e8s_equivalent` instead of `0` when proposals were settled, so the undistributed portion is always carried forward.

The same fix should be applied to the asynchronous `continue_processing()` path in `RewardsDistribution`.

### Proof of Concept

**NNS scenario:**

1. Neuron A (voting power = 300) and Neuron B (voting power = 100) both vote on Proposal P.
2. Proposal P is decided and enters `ReadyToSettle`.
3. Neuron A's owner calls `disburse()`, removing Neuron A from the neuron store.
4. The daily reward timer fires and calls `distribute_voting_rewards_to_neurons()`.
5. Inside `calculate_voting_rewards()`, `total_voting_rights = 400`, `total_available_e8s_equivalent = 100`.
6. Neuron A is not found → its 75 e8s share is silently dropped.
7. Neuron B receives only 25 e8s.
8. `e8s_equivalent_to_be_rolled_over()` returns `0` because `settled_proposals` is non-empty.
9. The next reward event starts with no rollover from the lost 75 e8s.
10. 75 e8s of maturity equivalent are permanently destroyed.

This is confirmed by the existing test `test_neuron_deleted_after_voting` in `rs/nns/governance/tests/governance.rs` which explicitly documents `distributed_e8s_equivalent: 25, total_available_e8s_equivalent: 100` as the expected (accepted) outcome. [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6722-6742)
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
```

**File:** rs/nns/governance/src/reward/calculation.rs (L120-147)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }

    /// Calculates the rounds_since_last_distribution in this event that should
    /// be "rolled over" into the next `RewardEvent`.
    ///
    /// Behavior:
    /// - If rewards were distributed for this event, then no rounds should be
    ///   rolled over, so this function returns 0.
    /// - Otherwise, this function returns
    ///   `rounds_since_last_distribution`.
    pub(crate) fn rounds_since_last_distribution_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.rounds_since_last_distribution.unwrap_or(0)
        } else {
            0
        }
    }

    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```

**File:** rs/nns/governance/src/reward/distribution.rs (L173-182)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L5954-5970)
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
```

**File:** rs/nns/governance/tests/governance.rs (L3235-3255)
```rust
    assert_eq!(
        *gov.latest_reward_event(),
        RewardEvent {
            day_after_genesis: 1,
            actual_timestamp_seconds: fake_driver.now(),
            settled_proposals: vec![ProposalId { id: 1 }],
            // We should have distributed 100 e8 equivalent if all voters still existed.
            // Since neuron 999 is gone and had a voting power 3x that of neuron 2,
            // only 1/4 is actually distributed.
            distributed_e8s_equivalent: 25,
            total_available_e8s_equivalent: 100,
            rounds_since_last_distribution: Some(1),
            latest_round_available_e8s_equivalent: Some(100)
        }
    );
    assert_eq!(
        25,
        gov.get_full_neuron(&NeuronId { id: 2 }, &principal(2))
            .unwrap()
            .maturity_e8s_equivalent
    );
```
