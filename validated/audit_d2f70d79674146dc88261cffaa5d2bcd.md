The code confirms all claims. The bug is real and verified:

1. `rewards_rolled_over()` at [1](#0-0)  uses only `settled_proposals.is_empty()` as its condition.
2. When `total_reward_shares == dec!(0)`, distribution is skipped but `settled_proposals` is still populated in the `RewardEvent` at [2](#0-1) .
3. Therefore `e8s_equivalent_to_be_rolled_over()` returns `0` at [3](#0-2) , and the next period's purse at [4](#0-3)  does not include the lost amount.

---

Audit Report

## Title
SNS Voting Rewards Permanently Lost When Proposals Settle in a Zero-Participation Period - (File: rs/sns/governance/src/governance.rs, rs/sns/governance/src/types.rs)

## Summary
In the SNS governance canister, when `distribute_rewards` runs and `total_reward_shares == 0` (no neuron voted on any settled proposal), the computed `rewards_purse_e8s` is neither distributed nor carried forward. The rollover guard `rewards_rolled_over()` checks only `settled_proposals.is_empty()`, which is `false` even when `distributed_e8s_equivalent == 0`, so the entire reward purse for that period is silently discarded and permanently unrecoverable.

## Finding Description
`distribute_rewards` computes `rewards_purse_e8s` seeded from `e8s_equivalent_to_be_rolled_over()` plus new per-round rewards (`rs/sns/governance/src/governance.rs` L5854–5876). When `total_reward_shares == dec!(0)`, the neuron maturity loop is skipped entirely, leaving `distributed_e8s_equivalent = 0` (`rs/sns/governance/src/governance.rs` L5946–5998). The function then constructs a `RewardEvent` with `settled_proposals` set to the non-empty list of processed proposals and `distributed_e8s_equivalent = 0` (`rs/sns/governance/src/governance.rs` L6084–6092).

On the next call, `rewards_purse_e8s` is seeded by `e8s_equivalent_to_be_rolled_over()` (`rs/sns/governance/src/governance.rs` L5855–5857), which delegates to `rewards_rolled_over()` in `rs/sns/governance/src/types.rs` L2065–2067:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
```

Because `settled_proposals` is non-empty, this returns `false`, and `e8s_equivalent_to_be_rolled_over()` returns `0` (`rs/sns/governance/src/types.rs` L2054–2060). The lost purse is never recovered. The NNS governance has the identical structural flaw in `rs/nns/governance/src/reward/calculation.rs` L144–147.

## Impact Explanation
This is a permanent, irreversible loss of SNS governance token maturity (staking rewards). The lost amount equals `rewards_purse_e8s` for the affected period — proportional to total SNS token supply multiplied by the configured reward rate. For any SNS with a non-trivial token supply and reward rate, this represents a material and permanent destruction of governance incentives. The affected proposals are also permanently marked `Settled` with `reward_event_round` set, so they can never be reconsidered. This matches the allowed High impact: "Significant SNS security impact with concrete user or protocol harm."

## Likelihood Explanation
The trigger requires only that proposals settle in a reward period where no neuron voted. This is realistic and easily triggered: any unprivileged user can submit a proposal to a newly launched SNS (where following relationships are not yet configured) and allow it to expire without votes. No special privileges are required. The condition is especially likely in the first reward round of a new SNS deployment.

## Recommendation
Change `rewards_rolled_over()` in `rs/sns/governance/src/types.rs` to check whether rewards were actually distributed, not merely whether proposals were settled:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.distributed_e8s_equivalent == 0
}
```

Apply the same fix to `rs/nns/governance/src/reward/calculation.rs`. This ensures any undistributed reward purse is always carried forward, regardless of whether proposals were settled.

## Proof of Concept
1. Deploy a new SNS with `voting_rewards_parameters` configured (non-zero reward rate).
2. Submit a governance proposal via an unprivileged ingress call.
3. Allow the proposal's voting period to expire with zero votes cast.
4. Wait for `run_periodic_tasks` to invoke `distribute_rewards` after the reward round ends.
5. Observe: `distribute_rewards` computes a non-zero `rewards_purse_e8s`, enters the `total_reward_shares == dec!(0)` branch, skips neuron maturity updates, and records a `RewardEvent` with `settled_proposals` non-empty and `distributed_e8s_equivalent == 0`.
6. Observe: on the next reward round, `e8s_equivalent_to_be_rolled_over()` returns `0` because `rewards_rolled_over()` returns `false` (settled_proposals is non-empty).
7. Confirm the next period's purse does not include the lost amount. The maturity is permanently gone.

This can be reproduced as a deterministic PocketIC integration test by configuring a short round duration, submitting a proposal, advancing time past the voting period and reward round boundary, and asserting that `latest_reward_event.distributed_e8s_equivalent == 0` while `latest_reward_event.total_available_e8s_equivalent > 0`, and that the subsequent reward event's purse does not include the prior period's undistributed amount.

### Citations

**File:** rs/sns/governance/src/types.rs (L2054-2060)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L2065-2067)
```rust
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```

**File:** rs/sns/governance/src/governance.rs (L5854-5858)
```rust
        let rewards_purse_e8s = {
            let mut result = Decimal::from(
                self.latest_reward_event()
                    .e8s_equivalent_to_be_rolled_over(),
            );
```

**File:** rs/sns/governance/src/governance.rs (L6084-6092)
```rust
        self.proto.latest_reward_event = Some(RewardEvent {
            round: new_reward_event_round,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent,
            end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
            rounds_since_last_distribution: Some(new_rounds_count),
            total_available_e8s_equivalent,
        })
```
