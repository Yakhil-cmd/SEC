The code confirms the claim at every step. Let me verify the NNS rollover behavior for comparison.

Audit Report

## Title
SNS Governance Rewards Purse Permanently Destroyed When `total_reward_shares` Is Zero With Settled Proposals - (File: rs/sns/governance/src/governance.rs)

## Summary
In `distribute_rewards`, when `total_reward_shares == dec!(0)` (no neuron cast an eligible vote on any considered proposal) but `considered_proposals` is non-empty, the function skips maturity distribution yet still settles every proposal and records a `RewardEvent` with a non-empty `settled_proposals` list. Because `rewards_rolled_over()` gates on `settled_proposals.is_empty()`, the entire `rewards_purse_e8s` accumulated for that round is neither distributed nor carried forward — it is silently and permanently destroyed.

## Finding Description
`distribute_rewards` (`rs/sns/governance/src/governance.rs`) computes `rewards_purse_e8s` starting from the previous event's `e8s_equivalent_to_be_rolled_over()` plus the current round's supply-weighted reward rate (L5854–5875). It then builds `neuron_id_to_reward_shares` by iterating over ballots of `considered_proposals`, counting only votes where `eligible_for_rewards()` is true (L5894–5931). If no neuron cast an eligible vote, `total_reward_shares` sums to `Decimal::ZERO`.

At L5946–5952, the zero-shares branch logs a warning and skips the distribution loop entirely — `distributed_e8s_equivalent` stays `0`. Execution then falls through unconditionally to the proposal-settlement loop at L6013–6081, which sets `reward_event_end_timestamp_seconds`, increments `reward_event_round`, and clears ballots for every proposal in `considered_proposals`. The resulting `RewardEvent` (L6084–6092) is stored with `settled_proposals: considered_proposals` — non-empty.

On the next call to `distribute_rewards`, `rewards_purse_e8s` is seeded from `self.latest_reward_event().e8s_equivalent_to_be_rolled_over()` (L5856–5858). That method (`rs/sns/governance/src/types.rs` L2054–2060) returns `total_available_e8s_equivalent` only when `rewards_rolled_over()` is `true`; `rewards_rolled_over()` (L2065–2067) returns `self.settled_proposals.is_empty()`. Because `settled_proposals` is non-empty, `e8s_equivalent_to_be_rolled_over()` returns `0`, and the prior round's entire purse is gone. The NNS exhibits the identical structural flaw (`rs/nns/governance/src/governance.rs` L6712–6719; `rs/nns/governance/src/reward/calculation.rs` L120–147).

Existing guards do not prevent this: the `total_reward_shares == dec!(0)` check only skips distribution; it does not skip settlement or force a rollover. There is no guard that prevents settling proposals when no rewards can be distributed.

## Impact Explanation
Every SNS governance token's worth of maturity accrued in `rewards_purse_e8s` for the affected round is permanently destroyed — it is neither credited to neurons nor carried to the next round. For an SNS with a meaningful token supply and non-trivial reward rate, this represents a concrete, irreversible loss of governance token maturity owed to neuron holders. The loss is silent (no on-chain error, no revert) and unrecoverable after the round closes. This matches the High severity category: **Significant SNS security impact with concrete user or protocol harm** — neuron holders are permanently deprived of maturity they are entitled to under the protocol's reward accounting.

## Likelihood Explanation
The trigger requires `considered_proposals` to be non-empty while `total_reward_shares == 0`. This arises whenever every proposal ready to settle received zero eligible votes in the reward window. Realistic scenarios include: (1) a newly launched SNS where neurons have not yet reached the minimum dissolve delay required to vote; (2) an SNS round where all neurons dissolved or were merged/split between proposal creation and settlement; (3) any round where proposals expire unvoted due to governance inactivity. SNS deployments are often small and lightly governed, making these conditions more probable than the analogous NNS case. No special privilege is required — a user meeting the proposal submission threshold can submit a proposal, and if participation is absent, the condition is met naturally.

## Recommendation
Before settling proposals, check whether `total_reward_shares == dec!(0)` and `considered_proposals` is non-empty. If so, either:
1. Skip settling proposals in this round (leave them `ReadyToSettle` for the next event), preserving the purse via the normal rollover path; or
2. After settling, override `e8s_equivalent_to_be_rolled_over` to return `total_available_e8s_equivalent` regardless of `settled_proposals`, so the purse is explicitly carried forward.

The simplest correct fix is option 1: add an early return (or `continue`) after the zero-shares warning log, before the proposal-settlement loop, so that proposals are not settled and the `RewardEvent` is not recorded when no rewards can be distributed.

## Proof of Concept
1. Deploy an SNS with `voting_rewards_parameters` configured (non-zero reward rate, finite round duration) and a non-zero token supply.
2. Ensure all neurons are below the minimum dissolve delay (or have fully dissolved) so none can cast eligible votes.
3. Submit a governance proposal. It receives pre-populated ballots but no neuron votes; it becomes `ReadyToSettle` after its voting deadline.
4. Advance time past one full reward round. `run_periodic_tasks` calls `distribute_rewards`.
5. Observe: `rewards_purse_e8s > 0` (supply × rate × duration); `neuron_id_to_reward_shares` is empty; `total_reward_shares == dec!(0)`.
6. The warning branch fires; distribution is skipped; the settlement loop runs; `RewardEvent.settled_proposals` is non-empty; `distributed_e8s_equivalent == 0`.
7. Advance time past a second reward round. Observe that the new `rewards_purse_e8s` starts from `0` (not from the prior round's `total_available_e8s_equivalent`), confirming the purse was destroyed.
8. A deterministic PocketIC or unit test can assert `latest_reward_event().e8s_equivalent_to_be_rolled_over() == 0` after step 6 and confirm the second round's purse equals only the fresh supply-based accrual, not the sum.