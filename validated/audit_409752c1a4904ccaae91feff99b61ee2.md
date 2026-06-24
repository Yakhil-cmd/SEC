Audit Report

## Title
Voting Reward Purse Permanently Discarded When Settled Proposals Receive Zero Votes — (`rs/sns/governance/src/governance.rs`, `rs/nns/governance/src/reward/calculation.rs`, `rs/sns/governance/src/types.rs`)

## Summary
Both NNS and SNS governance gate the rollover of the reward purse exclusively on whether `settled_proposals` is empty. When one or more proposals are settled in a reward round but no neuron cast a vote, `settled_proposals` is non-empty, `rewards_rolled_over()` returns `false`, and `e8s_equivalent_to_be_rolled_over()` returns 0. The entire computed reward purse for that round — including any previously accumulated rollover — is permanently discarded rather than carried forward.

## Finding Description

**Root cause — NNS** (`rs/nns/governance/src/reward/calculation.rs`, L144–147):
```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()   // only condition checked
}
```
`e8s_equivalent_to_be_rolled_over()` (L120–126) returns 0 whenever `rewards_rolled_over()` is `false`, i.e., whenever any proposal was settled — regardless of whether any rewards were actually distributed.

**Root cause — SNS** (`rs/sns/governance/src/types.rs`, L2065–2067):
```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()   // identical gate
}
```

**SNS zero-vote path** (`rs/sns/governance/src/governance.rs`, L5946–5952): When no neuron voted, `total_reward_shares == dec!(0)`, the guard branch logs a warning and skips all maturity increments. `distributed_e8s_equivalent` remains 0.

**SNS RewardEvent construction** (L6084–6092): The new `RewardEvent` is written with `settled_proposals: considered_proposals` (non-empty) and `distributed_e8s_equivalent: 0`. On the next call to `distribute_rewards`, the purse seed (L5854–5858) calls `self.latest_reward_event().e8s_equivalent_to_be_rolled_over()`, which returns 0 because `rewards_rolled_over()` is `false`. The lost purse is never recovered.

**NNS zero-vote path** (`rs/nns/governance/src/governance.rs`, L6696–6757): `voters_to_used_voting_right` is empty when no neuron voted; the reward loop never executes; `actually_distributed_e8s_equivalent` stays 0; yet `settled_proposals` is non-empty in the resulting `RewardEvent`, so the rollover gate fails identically.

No existing guard checks whether `distributed_e8s_equivalent == 0` before deciding to discard the purse.

## Impact Explanation
Every reward round in which at least one proposal is settled but zero votes are cast causes the entire computed reward purse (`supply × reward_rate × round_duration`, plus any previously rolled-over amount) to be permanently destroyed — no maturity is minted and the amount is not recovered in subsequent rounds. For SNS instances this directly reduces the total maturity that token holders can ever receive, constituting a concrete, permanent financial loss to SNS participants. This matches the allowed High impact: *"Significant SNS... security impact with concrete user or protocol harm."*

## Likelihood Explanation
For SNS the condition is realistic: SNS instances frequently launch with a small neuron set and no mandatory default-followee graph. A proposal whose voting period expires before any neuron votes — due to low participation, a misconfigured dissolve-delay threshold, or deliberate abstention — directly triggers the bug. Any SNS token holder with sufficient stake to submit a proposal can cause this; no special privilege is required. The NNS is less likely to be affected due to its default-followee system, but the code path is identical and reachable in principle.

## Recommendation
Change `rewards_rolled_over()` to also return `true` when proposals were settled but nothing was actually distributed:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty() || self.distributed_e8s_equivalent == 0
}
```

Apply the same fix to both the NNS implementation in `rs/nns/governance/src/reward/calculation.rs` and the SNS implementation in `rs/sns/governance/src/types.rs`. This ensures the reward purse is always carried forward when no maturity was actually minted, regardless of whether proposals were settled.

## Proof of Concept
1. Deploy an SNS with `initial_reward_rate_basis_points > 0` and a finite `round_duration_seconds`.
2. Create a proposal using a neuron with sufficient dissolve delay and stake.
3. Allow the proposal's voting period to expire without any neuron casting a vote.
4. Advance time past the reward round boundary so `run_periodic_tasks` calls `distribute_rewards`.
5. Observe: `considered_proposals` is non-empty; `total_reward_shares == dec!(0)`; warning is logged; `distributed_e8s_equivalent = 0`; `settled_proposals` is non-empty in the new `RewardEvent`.
6. Advance time to the next reward round and call `distribute_rewards` again.
7. Observe: `e8s_equivalent_to_be_rolled_over()` returns 0 (because `rewards_rolled_over()` was `false`); the next round's `rewards_purse_e8s` does not include the prior round's lost amount.
8. Confirm `latest_reward_event.total_available_e8s_equivalent > 0` while `distributed_e8s_equivalent == 0`, and the following round's purse is computed as if the prior round never existed.

This is reproducible as a deterministic PocketIC integration test by controlling the clock and neuron configuration.