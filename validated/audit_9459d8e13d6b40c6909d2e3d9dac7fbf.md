Audit Report

## Title
Off-by-One in SNS Governance Voting Deadline Allows Vote at Exact Deadline Second - (File: `rs/sns/governance/src/governance.rs`)

## Summary
The `register_vote` function in SNS governance uses `now_seconds > deadline` to gate vote acceptance, while the protocol's canonical `accepts_vote` helper uses `now_seconds < deadline`. At exactly `now_seconds == deadline`, `register_vote` does not return an error and proceeds to permanently record the ballot via `cast_vote_and_cascade_follow`, even though `accepts_vote` simultaneously returns `false` for that timestamp. The NNS governance correctly delegates to `accepts_vote` and does not have this inconsistency.

## Finding Description
In `rs/sns/governance/src/governance.rs` at L3914–3922, the deadline guard is:

```rust
let deadline = proposal.get_deadline_timestamp_seconds();
if now_seconds > deadline {
    return Err(...);
}
``` [1](#0-0) 

The protocol's canonical definition of "still accepting votes" is in `rs/sns/governance/src/proposal.rs` at L2100–2103:

```rust
pub fn accepts_vote(&self, now_seconds: u64) -> bool {
    now_seconds < self.get_deadline_timestamp_seconds()
}
``` [2](#0-1) 

When `now_seconds == deadline`, the `>` guard in `register_vote` evaluates to `false` (no error), execution continues, and `cast_vote_and_cascade_follow` is called at L3931–3942, permanently writing the ballot and triggering `process_proposal`. [3](#0-2) 

By contrast, NNS governance at L5628–5637 correctly delegates to `accepts_vote` (which uses `<`), rejecting votes at exactly the deadline:

```rust
let accepts_vote = proposal.accepts_vote(now_seconds, voting_period_seconds);
if !accepts_vote { return Err(...); }
``` [4](#0-3) 

The SNS `register_vote` does not call `accepts_vote` at all — it has its own inline check with the wrong operator. [5](#0-4) 

## Impact Explanation
This is a **High** severity finding. Any neuron holder with a ballot on an SNS proposal can cast a vote at exactly `now_seconds == deadline` — a timestamp at which the voting period has officially ended per the protocol's own `accepts_vote` definition. The late vote is permanently recorded and included in the tally used by `process_proposal` to decide the proposal outcome. A large token holder can observe the live tally (publicly readable via query calls), wait until the exact deadline second, and cast a decisive vote to flip a close outcome after all other participants believe the window has closed. This constitutes a concrete governance integrity impact on SNS deployments, matching the allowed impact class: *Significant SNS security impact with concrete user or protocol harm*.

## Likelihood Explanation
The Internet Computer's consensus layer advances `now_seconds` in discrete rounds tied to Unix timestamps. An attacker can monitor the on-chain time and the stored `current_deadline_timestamp_seconds` of a proposal, then submit an ingress message in the round where the two values are equal. No privileged access is required — any principal controlling a neuron with an uncast ballot on the proposal can perform this. The attack is deterministic, repeatable across every SNS deployment, and requires no victim cooperation or external dependency.

## Recommendation
Replace the inline `>` comparison in `register_vote` with the canonical `accepts_vote` helper, consistent with the NNS pattern:

```rust
// Before (buggy):
if now_seconds > deadline { ... }

// After (correct):
if !proposal.accepts_vote(now_seconds) { ... }
```

This eliminates the duplicate, inconsistent deadline definition and ensures a single authoritative check governs vote acceptance throughout the SNS governance canister. [1](#0-0) 

## Proof of Concept
1. Deploy an SNS with a proposal whose `current_deadline_timestamp_seconds = T`.
2. At time `T − 1`, observe the tally is close (e.g., 50.1% Yes).
3. Hold a neuron with a No ballot that has not yet voted.
4. At time `T` (exactly the deadline), submit `manage_neuron { RegisterVote { proposal_id, vote: No } }`.
5. `register_vote` evaluates `T > T` → `false` → no error returned.
6. `cast_vote_and_cascade_follow` records the No vote; `process_proposal` is called and the proposal is decided as Rejected.
7. Verify: call `accepts_vote(T)` on the same proposal — it returns `false`, confirming the vote was accepted outside the canonical voting window.

A deterministic integration test using PocketIC can set the mock time to exactly `T` and assert that `register_vote` succeeds while `accepts_vote` returns `false` for the same timestamp. [2](#0-1)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L3931-3944)
```rust
        Governance::cast_vote_and_cascade_follow(
            proposal_id,
            neuron_id,
            vote,
            function_id,
            &self.function_followee_index,
            &self.topic_follower_index,
            &self.proto.neurons,
            now_seconds,
            &mut proposal.ballots,
            proposal_topic.unwrap_or_default(),
        );

        self.process_proposal(proposal_id.id);
```

**File:** rs/sns/governance/src/proposal.rs (L2100-2103)
```rust
    pub fn accepts_vote(&self, now_seconds: u64) -> bool {
        // Checks if the proposal's deadline is still in the future.
        now_seconds < self.get_deadline_timestamp_seconds()
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
