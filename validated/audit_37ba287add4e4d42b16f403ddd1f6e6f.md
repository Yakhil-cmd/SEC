Audit Report

## Title
Voting Reward Maturity Permanently Destroyed When Neuron Dissolved Before Distribution - (File: rs/nns/governance/src/governance.rs)

## Summary
In NNS and SNS governance, when a neuron votes on a proposal and is subsequently dissolved and removed from the neuron store before the reward distribution period executes, its proportional share of the reward purse is permanently destroyed. The `total_available_e8s_equivalent` is fully consumed from the inflation allocation, but `distributed_e8s_equivalent` is lower by the skipped share. The gap is never rolled over to the next reward period, never redistributed to other voters, and never returned to any treasury — it is irrecoverably lost.

## Finding Description

**NNS — `calculate_voting_rewards()` in `rs/nns/governance/src/governance.rs` (L6722–6742):**

When iterating over voters to build the reward distribution, if a neuron voted but is no longer present in the neuron store, its share is silently skipped and `actually_distributed_e8s_equivalent` is not incremented for that neuron:

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
            LOG_PREFIX, neuron_id.id, used_voting_rights
        );
    }
}
```

The rollover logic in `rs/nns/governance/src/reward/calculation.rs` (L120–147) only activates when `settled_proposals.is_empty()`:

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
```

In the next reward event, `rolling_over_from_previous_reward_event_e8s_equivalent` is computed from `e8s_equivalent_to_be_rolled_over()` (L6651–6654 of `governance.rs`). When proposals ARE settled (the normal distribution case), this returns `0`, so the gap between `total_available_e8s_equivalent` and `distributed_e8s_equivalent` is never carried forward.

**NNS — async `continue_processing()` in `rs/nns/governance/src/reward/distribution.rs` (L173–182):**

A second loss path exists in the asynchronous distribution task. If a neuron is present in `RewardsDistributionInProgress` but is removed from the neuron store between scheduling and processing, the reward is silently dropped with only a log message.

**SNS — `distribute_rewards()` in `rs/sns/governance/src/governance.rs` (L5954–5970):**

The identical pattern exists in SNS governance — a missing neuron causes `continue`, skipping the reward with no rollover.

**Root cause:** `e8s_equivalent_to_be_rolled_over()` unconditionally returns `0` whenever `settled_proposals` is non-empty, regardless of whether the full `total_available_e8s_equivalent` was actually distributed. The gap `total_available_e8s_equivalent - distributed_e8s_equivalent` is permanently destroyed.

## Impact Explanation

Voting rewards (maturity equivalents) allocated to neurons that no longer exist at distribution time are permanently destroyed. They are not redistributed to other voters, not rolled over to the next reward period, and not returned to any treasury. The total effective reward supply is permanently reduced by the missing share. This constitutes a concrete, permanent loss of in-scope NNS/SNS governance assets (maturity equivalents, which are redeemable for ICP). The impact scales with the dissolved neuron's voting power relative to total voting power — a large neuron with significant delegated following could cause a substantial fraction of the period's entire reward purse to be destroyed. This matches the **High** impact class: "Significant NNS, SNS, or infrastructure security impact with concrete user or protocol harm."

## Likelihood Explanation

Any unprivileged neuron owner can trigger this condition without special access:
1. Vote on a proposal (directly or via following).
2. Wait for the proposal to reach `ReadyToSettle` state.
3. Dissolve and disburse the neuron — a standard, permissionless operation — before the daily reward distribution timer fires.

The window is up to one full reward period (one day in NNS, configurable in SNS). No governance majority, no admin key, and no privileged access is required. The operation is entirely normal user behavior. The larger the dissolved neuron's voting power relative to the total, the larger the permanent loss. This is repeatable every reward period.

## Recommendation

When a neuron's reward share cannot be distributed because the neuron no longer exists, the undistributed amount should be rolled over to the next reward period rather than being permanently destroyed:

- In `calculate_voting_rewards()` (NNS) and `distribute_rewards()` (SNS), accumulate skipped reward amounts into a `skipped_e8s` counter.
- Subtract `skipped_e8s` from `actually_distributed_e8s_equivalent` so the `RewardEvent` accurately reflects what was credited.
- Update `e8s_equivalent_to_be_rolled_over()` to return `total_available_e8s_equivalent - distributed_e8s_equivalent` instead of `0` when proposals were settled, ensuring the undistributed portion is always carried forward.
- Apply the same fix to the asynchronous `continue_processing()` path in `RewardsDistributionInProgress`.

## Proof of Concept

The existing test in `rs/nns/governance/tests/governance.rs` (L3235–3255) explicitly confirms and accepts this behavior:

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
```

The 75 e8s are permanently lost — not rolled over to the next event. To reproduce:
1. Create Neuron A (voting power 300) and Neuron B (voting power 100); both vote on Proposal P.
2. Proposal P reaches `ReadyToSettle`.
3. Dissolve and disburse Neuron A, removing it from the neuron store.
4. Advance time past the reward distribution period and trigger the reward timer.
5. Observe `distributed_e8s_equivalent: 25, total_available_e8s_equivalent: 100` in the `RewardEvent`.
6. Confirm the next reward event's `rolling_over_from_previous_reward_event_e8s_equivalent` is `0` (not 75), confirming permanent destruction.