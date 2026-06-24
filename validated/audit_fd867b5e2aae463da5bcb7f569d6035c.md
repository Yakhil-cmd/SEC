Audit Report

## Title
SNS Governance `distribute_rewards` Collects Proposals Using Wall-Clock `now` Instead of Round-End Timestamp — (`rs/sns/governance/src/governance.rs`)

## Summary
In SNS governance, `distribute_rewards` collects proposals to settle by calling `ready_to_be_settled_proposal_ids()`, which internally snapshots `self.env.now()` (actual execution time) rather than `reward_event_end_timestamp_seconds` (the nominal round boundary). Because `distribute_rewards` always fires some time after the round nominally ends, proposals whose voting deadlines fall in the gap `(reward_event_end_timestamp_seconds, now]` are incorrectly settled in the current reward event. NNS governance avoids this exact issue by passing the round-end timestamp explicitly. The result is that the current round's reward purse is diluted across more proposals than intended, and affected proposals are permanently marked `Settled`, so their voters never receive rewards from the correct round.

## Finding Description
In `rs/sns/governance/src/governance.rs`, `distribute_rewards` captures wall-clock time at entry:

```rust
// line 5765
let now = self.env.now();
```

Proposals are collected at line 5822–5823 via `ready_to_be_settled_proposal_ids()`, which has no timestamp parameter and internally calls `self.env.now()` again:

```rust
// lines 1927–1934
fn ready_to_be_settled_proposal_ids(&self) -> impl Iterator<Item = ProposalId> + '_ {
    let now = self.env.now();
    self.proto.proposals.iter()
        .filter(move |(_, data)| data.reward_status(now) == ProposalRewardStatus::ReadyToSettle)
        .map(|(k, _)| ProposalId { id: *k })
}
```

`reward_status` returns `ReadyToSettle` when `!accepts_vote(now)`, i.e., when `now >= get_deadline_timestamp_seconds()` (proposal.rs line 2100–2102). The nominal round boundary `reward_event_end_timestamp_seconds` is only computed *after* the proposal collection, at lines 5837–5839:

```rust
let reward_event_end_timestamp_seconds = new_rounds_count
    .saturating_mul(round_duration_seconds)
    .saturating_add(reward_start_timestamp_seconds);
```

Any proposal P with `reward_event_end_timestamp_seconds < deadline(P) <= now` passes the `ReadyToSettle` filter using `now` but would still be `AcceptVotes` if evaluated at `reward_event_end_timestamp_seconds`. P is then settled in the current round (line 6013–6020), marked with `reward_event_end_timestamp_seconds`, and will never appear in the next round.

NNS governance avoids this by computing the round-end timestamp first and passing it explicitly (nns/governance/src/governance.rs lines 6658–6662):

```rust
let as_of_timestamp_seconds =
    self.most_recent_fully_elapsed_reward_round_end_timestamp_seconds();
let considered_proposals: Vec<ProposalId> = self
    .ready_to_be_settled_proposal_ids(as_of_timestamp_seconds)
    .collect();
```

No guard in `distribute_rewards` prevents this early inclusion; the only guard (line 5815–5819) checks `new_rounds_count == 0`, which is unrelated.

## Impact Explanation
The current round's reward purse (correctly computed from token supply × rate × elapsed rounds) is distributed across a superset of the intended proposals. Neurons that voted on proposals legitimately belonging to the current round receive a diluted share of the purse. The prematurely settled proposals are permanently marked `Settled` and excluded from the next round, so their voters receive rewards from the wrong round's purse and the next round's purse is not correspondingly increased. This constitutes concrete, repeated financial harm to SNS governance participants — a significant SNS security impact with measurable per-round reward accounting errors. This maps to **High ($2,000–$10,000): Significant SNS security impact with concrete user or protocol harm**.

## Likelihood Explanation
The bug triggers on every invocation of `distribute_rewards` where any proposal's deadline falls in the execution gap. The gap is typically seconds to minutes (timer jitter). An unprivileged SNS token holder who can submit proposals can deliberately time a proposal's deadline (accounting for wait-for-quiet extensions) to fall in this window by submitting the proposal approximately one voting period before the round boundary. The round schedule is deterministic and publicly observable, making the timing predictable. No special privileges are required beyond the standard SNS proposal submission stake.

## Recommendation
Compute `reward_event_end_timestamp_seconds` before collecting proposals, then refactor `ready_to_be_settled_proposal_ids` to accept a timestamp parameter (mirroring the NNS implementation):

```rust
let reward_event_end_timestamp_seconds = new_rounds_count
    .saturating_mul(round_duration_seconds)
    .saturating_add(reward_start_timestamp_seconds);

let considered_proposals: Vec<ProposalId> =
    self.ready_to_be_settled_proposal_ids(reward_event_end_timestamp_seconds).collect();
```

And update the function signature:

```rust
fn ready_to_be_settled_proposal_ids(&self, as_of_timestamp_seconds: u64)
    -> impl Iterator<Item = ProposalId> + '_
```

## Proof of Concept
1. Deploy an SNS with `round_duration_seconds = 604800` (7 days). Last reward event ended at `T`.
2. Submit proposal P such that its voting deadline (including any wait-for-quiet extension) is `T + 15s`.
3. Wait for `distribute_rewards` to fire at `T + 30s` (i.e., `now = T + 30s`).
4. `reward_event_end_timestamp_seconds` = `T` (correct boundary).
5. `ready_to_be_settled_proposal_ids()` evaluates `reward_status(now = T+30s)`: since `T+30s >= T+15s`, P is `ReadyToSettle` and included in `considered_proposals`.
6. Correct behavior: evaluated at `T`, `T < T+15s`, so P is still `AcceptVotes` and should belong to the next round.
7. P is settled in the current round (line 6013), marked `Settled`, and excluded from the next round.
8. The current round's purse is diluted; neurons that voted on legitimately current-round proposals receive less maturity than entitled.

A deterministic unit test can reproduce this by mocking `env.now()` to return `T + 30s`, setting `latest_reward_event().end_timestamp_seconds = T`, and asserting that P (with deadline `T + 15s`) is incorrectly included in `considered_proposals`.