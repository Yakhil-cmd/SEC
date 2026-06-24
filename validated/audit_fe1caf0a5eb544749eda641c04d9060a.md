Audit Report

## Title
Off-by-One Boundary Condition in NNS Governance `accepts_vote` Silently Rejects Votes at Exact Deadline - (File: rs/nns/governance/src/governance.rs)

## Summary
`ProposalData::accepts_vote` in NNS Governance uses a strict `<` comparison, causing the deadline second itself to be excluded from the valid voting window. A neuron vote processed at exactly `now_seconds == deadline` is permanently rejected with `PreconditionFailed`, while the SNS Governance counterpart uses `now_seconds > deadline` and correctly accepts votes at the boundary. The inconsistency is confirmed in production code and the existing test deliberately skips the exact boundary second, leaving the defect undetected.

## Finding Description
`accepts_vote` at [1](#0-0)  evaluates `now_seconds < self.get_deadline_timestamp_seconds(voting_period_seconds)`, producing the half-open interval `[creation, deadline)`. At `now_seconds == deadline` this returns `false`.

`register_vote` at [2](#0-1)  immediately propagates that `false` as a hard `PreconditionFailed` error with no retry path, permanently discarding the vote.

The SNS counterpart at [3](#0-2)  uses `now_seconds > deadline`, so at `now_seconds == deadline` the condition is `false` and the vote proceeds — the opposite, correct behavior.

`evaluate_wait_for_quiet` at [4](#0-3)  uses `now_seconds > current_deadline` (strict `>`), meaning WFQ would still run at the boundary second — but it is never reached because `accepts_vote` gates the entire path before `cast_vote_and_cascade_follow` is called.

The test `test_no_voting_after_deadline` advances time to `deadline_seconds + 1`, deliberately skipping `now_seconds == deadline` and leaving the boundary untested.

## Impact Explanation
A neuron vote processed at exactly the deadline second is permanently lost with no retry. If the rejected vote would have been the deciding vote on a close NNS proposal, the proposal outcome is incorrect. NNS governance controls protocol upgrades, subnet configuration, and ICP treasury disbursements; a single incorrectly decided proposal can have protocol-wide consequences. This matches the allowed impact: **Medium — significant NNS governance security impact with concrete user or protocol harm, requiring strict target conditions (exact one-second window)**.

## Likelihood Explanation
The deadline is public on-chain. For a standard 4-day voting period the accidental probability is ~1/345,600 per vote. However, the deadline second is predictable, so an adversary who can observe a large neuron's pending vote transaction and apply a targeted resource-exhaustion or ingress-delay attack can push that transaction into the boundary second. This requires meaningful per-target work (mempool monitoring plus precise delay), placing it in the Medium tier rather than High.

## Recommendation
Change the strict `<` to `<=` in `accepts_vote`:

```rust
pub fn accepts_vote(&self, now_seconds: u64, voting_period_seconds: u64) -> bool {
    now_seconds <= self.get_deadline_timestamp_seconds(voting_period_seconds)
}
```

This aligns NNS Governance with SNS Governance semantics and makes the deadline second inclusive. Add a unit test that advances time to exactly `deadline_seconds` (not `deadline_seconds + 1`) and asserts the vote is accepted.

## Proof of Concept
1. Create a proposal at `T0` with `voting_period_seconds = V`; deadline `D = T0 + V`.
2. Submit a `register_vote` ingress message timed so the replica processes it at batch time `now_seconds = D`.
3. `accepts_vote(D, V)` evaluates `D < D` → `false`.
4. `register_vote` returns `Err(PreconditionFailed, "Proposal deadline has passed.")`.
5. The neuron's vote is permanently dropped.
6. The same vote at `now_seconds = D - 1` evaluates `D-1 < D` → `true` and succeeds.

Minimal deterministic test: in `rs/nns/governance/tests/governance.rs`, replace `advance_time_by(deadline_seconds + 1 - DEFAULT_TEST_START_TIMESTAMP_SECONDS)` with `advance_time_by(deadline_seconds - DEFAULT_TEST_START_TIMESTAMP_SECONDS)` and assert that `register_vote` returns `Ok` — under the current code it returns `Err`, confirming the defect.

### Citations

**File:** rs/nns/governance/src/governance.rs (L612-623)
```rust
    pub fn accepts_vote(&self, now_seconds: u64, voting_period_seconds: u64) -> bool {
        // Naive version of the wait-for-quiet mechanics. For now just tests
        // that the proposal duration is smaller than the threshold, which
        // we're just currently setting as seconds.
        //
        // Wait for quiet is meant to be able to decide proposals without
        // quorum. The tally must have been done above already.
        //
        // If the wait for quit threshold is unset (0), then proposals can
        // accept votes forever.
        now_seconds < self.get_deadline_timestamp_seconds(voting_period_seconds)
    }
```

**File:** rs/nns/governance/src/governance.rs (L649-654)
```rust
        if new_tally.yes >= deciding_amount_yes
            || new_tally.no >= deciding_amount_no
            || now_seconds > current_deadline
        {
            return;
        }
```

**File:** rs/nns/governance/src/governance.rs (L5628-5637)
```rust
        // Check if the proposal is still open for voting.
        let voting_period_seconds = voting_period_seconds(topic);
        let accepts_vote = proposal.accepts_vote(now_seconds, voting_period_seconds);
        if !accepts_vote {
            // Deadline has passed, so the proposal cannot be voted on
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Proposal deadline has passed.",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L3914-3922)
```rust
        // Check if the proposal is still open for voting.
        let deadline = proposal.get_deadline_timestamp_seconds();
        if now_seconds > deadline {
            // Deadline has passed, so the proposal cannot be voted on
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Proposal deadline has passed.",
            ));
        }
```
