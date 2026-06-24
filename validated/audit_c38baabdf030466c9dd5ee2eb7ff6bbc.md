Audit Report

## Title
SNS Governance Voting Rewards Permanently Lost When Proposals Settle With Zero Voter Participation - (`rs/sns/governance/src/types.rs`, `rs/sns/governance/src/governance.rs`)

## Summary
In SNS governance, `rewards_rolled_over()` returns `true` only when `settled_proposals.is_empty()`. When a reward round contains proposals that reach `ReadyToSettle` but receive zero eligible votes, `distributed_e8s_equivalent` stays 0 while `settled_proposals` is non-empty. The next round's rollover calculation therefore returns 0, permanently discarding the entire rewards purse for that round. The identical structural flaw exists in NNS governance.

## Finding Description
The root cause is in `rewards_rolled_over()` in `rs/sns/governance/src/types.rs` (line 2065–2067):

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.settled_proposals.is_empty()
}
```

This predicate checks only whether proposals were settled, not whether any rewards were actually distributed. The full exploit path:

1. `distribute_rewards` in `rs/sns/governance/src/governance.rs` computes `rewards_purse_e8s` starting from `self.latest_reward_event().e8s_equivalent_to_be_rolled_over()` (line 5856–5857), then adds new round rewards (line 5871).
2. It tallies `total_reward_shares` from ballots where `vote.eligible_for_rewards()` is true (lines 5896–5930). If all ballots have `Vote::Unspecified`, `total_reward_shares == dec!(0)`.
3. The `if total_reward_shares == dec!(0)` branch (line 5946) logs an error and skips all maturity increments; `distributed_e8s_equivalent` remains 0.
4. Proposals in `considered_proposals` are still settled unconditionally (lines 6009–6081): `reward_event_end_timestamp_seconds` is set and ballots are cleared.
5. A new `RewardEvent` is written (lines 6084–6092) with `settled_proposals: considered_proposals` (non-empty), `distributed_e8s_equivalent: 0`, and `total_available_e8s_equivalent: Some(rewards_purse_e8s)`.
6. In the next round, `e8s_equivalent_to_be_rolled_over()` (types.rs lines 2054–2060) calls `rewards_rolled_over()`, which returns `false` because `settled_proposals` is non-empty, so it returns 0. The entire purse — including any previously accumulated rollover — is silently discarded.

The NNS has the identical flaw in `rs/nns/governance/src/reward/calculation.rs` (lines 144–147).

## Impact Explanation
This constitutes a permanent, unrecoverable loss of SNS voting rewards. Tokens that should have been minted as neuron maturity are never created, and the shortfall cannot be recovered in subsequent rounds. The `total_available_e8s_equivalent` field in the emitted `RewardEvent` records the full purse, making the loss observable on-chain but not correctable. This matches the allowed High impact: "Significant SNS security impact with concrete user or protocol harm." For a newly launched SNS, multiple rounds of accumulated rewards can be destroyed before any neuron holders configure following or cast votes.

## Likelihood Explanation
For SNS, the condition is reachable without any special privilege beyond holding a developer neuron (which is created at SNS genesis). During the decentralization swap and immediately after, swap participants have not yet configured following chains. A developer neuron holder can submit a proposal with a voting period shorter than `round_duration_seconds`; if no other neurons vote before the proposal reaches `ReadyToSettle`, the round's entire purse is lost. No governance majority or subnet-level access is required. The condition can be triggered repeatedly across consecutive rounds. For NNS, the large active neuron set with established following chains makes `total_voting_rights < 0.001` extremely unlikely in practice.

## Recommendation
Change `rewards_rolled_over()` in both SNS (`rs/sns/governance/src/types.rs`) and NNS (`rs/nns/governance/src/reward/calculation.rs`) to check whether rewards were actually distributed rather than whether proposals were settled:

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.distributed_e8s_equivalent == 0
}
```

This ensures any round in which no maturity was minted — whether due to no proposals or no eligible votes — carries its full purse forward to the next round.

## Proof of Concept
1. Deploy a local SNS (PocketIC or local replica). At genesis, `latest_reward_event` is initialized with `settled_proposals: vec![]` and `end_timestamp_seconds: Some(now)` (governance.rs lines 726–742).
2. Submit a proposal from a developer neuron with a voting period shorter than `round_duration_seconds`. No other neurons vote.
3. Advance time past the proposal's voting period so it reaches `ReadyToSettle`.
4. Advance time past `round_duration_seconds` so `should_distribute_rewards()` returns `true` (governance.rs lines 5725–5753).
5. Trigger `distribute_rewards`. Observe: `total_reward_shares == 0`, no maturity is minted, but the new `RewardEvent` has `settled_proposals = [proposal_1]` and `distributed_e8s_equivalent = 0` with `total_available_e8s_equivalent = Some(rewards_purse_e8s > 0)`.
6. Advance time past the next `round_duration_seconds`. Trigger `distribute_rewards` again. Observe: `e8s_equivalent_to_be_rolled_over()` returns 0 (because `rewards_rolled_over()` is `false`), and the new round's purse starts from 0 rollover — confirming the prior round's purse is permanently lost.
7. Assert invariant: sum of all `distributed_e8s_equivalent` across all `RewardEvent`s plus the current rollover should equal the sum of all `total_available_e8s_equivalent` values. This invariant is violated.