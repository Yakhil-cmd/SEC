### Title
Voting Reward Purse Permanently Lost When Voting Neurons Are Dissolved Before Reward Distribution — (`rs/nns/governance/src/governance.rs`)

---

### Summary

In NNS (and SNS) governance, when a neuron votes on a proposal and is subsequently dissolved and disbursed before the reward event fires, its proportional share of the voting reward purse is permanently lost. The undistributed portion is neither minted as maturity nor rolled over to the next reward round. This is the direct IC analog of the Velodrome `BribeVotingReward` bug: rewards allocated to an epoch with no eligible recipients are irrecoverably consumed.

---

### Finding Description

In `calculate_voting_rewards` in `rs/nns/governance/src/governance.rs`, the reward distribution loop iterates over `voters_to_used_voting_right` — a map built from the ballots stored in settled proposals. For each voter, the code checks whether the neuron still exists in the neuron store:

```rust
for (neuron_id, used_voting_rights) in voters_to_used_voting_right {
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
            ...
        );
    }
}
``` [1](#0-0) 

The skipped reward is not added to `actually_distributed_e8s_equivalent`. The resulting `RewardEvent` records `settled_proposals` as non-empty (the proposal was settled), so the rollover guard in `e8s_equivalent_to_be_rolled_over` returns `0`:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
    if self.rewards_rolled_over() {
        self.total_available_e8s_equivalent
    } else {
        0
    }
}
``` [2](#0-1) 

Because `settled_proposals` is non-empty, `rewards_rolled_over()` is `false`, and `e8s_equivalent_to_be_rolled_over()` returns `0`. The gap between `total_available_e8s_equivalent` and `actually_distributed_e8s_equivalent` is permanently lost — never minted, never rolled over.

The extreme case occurs when `total_voting_rights < 0.001` (all voting neurons are gone): `reward_distribution` is `None`, zero maturity is distributed, yet the proposals are still marked as settled, consuming the entire reward purse with no rollover:

```rust
let reward_distribution = if total_voting_rights < 0.001 {
    println!(
        "{}WARNING: total_voting_rights == {}, even though considered_proposals \
         is nonempty (see earlier log). Therefore, we skip incrementing maturity \
         to avoid dividing by zero (or super small number).",
        ...
    );
    None
``` [3](#0-2) 

The same pattern exists in SNS governance:

```rust
if total_reward_shares == dec!(0) {
    log!(ERROR, "Warning: total_reward_shares is 0. Therefore, we skip increasing \
         neuron maturity. ...");
} else { ... }
``` [4](#0-3) 

The existing test at `rs/nns/governance/tests/governance.rs` explicitly confirms this behavior — 75 of 100 available e8s are permanently lost when neuron 999 is gone:

```
// We should have distributed 100 e8 equivalent if all voters still existed.
// Since neuron 999 is gone and had a voting power 3x that of neuron 2,
// only 1/4 is actually distributed.
distributed_e8s_equivalent: 25,
total_available_e8s_equivalent: 100,
``` [5](#0-4) 

---

### Impact Explanation

**Medium.** The voting reward purse — computed as a fraction of the total ICP supply — is partially or wholly consumed without being minted as maturity for any neuron, and without being rolled over to the next round. The protocol's intended reward conservation invariant (`distributed_e8s_equivalent == total_available_e8s_equivalent` for settled rounds) is violated. The undistributed maturity is permanently unrecoverable. In the total-loss case (`total_voting_rights < 0.001`), the entire round's reward purse is silently discarded.

---

### Likelihood Explanation

**Low.** The trigger requires a neuron to vote on a proposal and then be fully dissolved and disbursed (removed from the neuron store) before the daily reward event fires. NNS neurons typically have long dissolve delays (months to years), making the window narrow. However, it is a normal, permissionless user action (dissolve + disburse) that requires no privileged access. For SNS governance, where dissolve delays can be shorter, the likelihood is slightly higher. The partial-loss case (some but not all voting neurons gone) is more likely than the total-loss case.

---

### Recommendation

When a voting neuron cannot be found at reward distribution time, its proportional share of `total_available_e8s_equivalent` should be added to the next round's rollover purse rather than silently discarded. Concretely, track the sum of skipped rewards and add it to the rolled-over amount in the next `RewardEvent`, analogous to the `renotifyRewardAmount` pattern suggested in the external report.

---

### Proof of Concept

The existing test at `rs/nns/governance/tests/governance.rs` already demonstrates the loss:

1. Neuron 999 votes on proposal 1 with voting power 3× that of neuron 2.
2. Neuron 999 is deleted before the reward event.
3. `distribute_voting_rewards_to_neurons` fires.
4. `total_available_e8s_equivalent = 100`, but `distributed_e8s_equivalent = 25`.
5. The 75 e8s allocated to neuron 999 are permanently lost — not minted, not rolled over. [6](#0-5) 

The rollover guard confirms no recovery is possible: because `settled_proposals` is non-empty, `e8s_equivalent_to_be_rolled_over()` returns `0` for this event, and the next round's purse does not include the lost amount. [2](#0-1)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6712-6719)
```rust
        let reward_distribution = if total_voting_rights < 0.001 {
            println!(
                "{}WARNING: total_voting_rights == {}, even though considered_proposals \
                 is nonempty (see earlier log). Therefore, we skip incrementing maturity \
                 to avoid dividing by zero (or super small number).",
                LOG_PREFIX, total_voting_rights,
            );
            None
```

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

**File:** rs/sns/governance/src/governance.rs (L5946-5952)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
```

**File:** rs/nns/governance/tests/governance.rs (L3241-3249)
```rust
            // We should have distributed 100 e8 equivalent if all voters still existed.
            // Since neuron 999 is gone and had a voting power 3x that of neuron 2,
            // only 1/4 is actually distributed.
            distributed_e8s_equivalent: 25,
            total_available_e8s_equivalent: 100,
            rounds_since_last_distribution: Some(1),
            latest_round_available_e8s_equivalent: Some(100)
        }
    );
```
