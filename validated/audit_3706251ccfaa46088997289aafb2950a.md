### Title
Off-by-One Timestamp Boundary in SNS Governance `register_vote` Allows Voting at Exact Deadline - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance `register_vote` function uses a strict `>` comparison to check whether the voting deadline has passed, while the canonical `accepts_vote` predicate uses a strict `<` comparison. This inconsistency creates a one-second window at exactly `now_seconds == deadline` where a neuron can cast a vote that `accepts_vote` considers closed, potentially flipping the outcome of a governance proposal.

### Finding Description
In `rs/sns/governance/src/governance.rs`, the `register_vote` function checks:

```rust
let deadline = proposal.get_deadline_timestamp_seconds();
if now_seconds > deadline {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Proposal deadline has passed.",
    ));
}
``` [1](#0-0) 

The canonical `accepts_vote` predicate in `rs/sns/governance/src/proposal.rs` is defined as:

```rust
pub fn accepts_vote(&self, now_seconds: u64) -> bool {
    now_seconds < self.get_deadline_timestamp_seconds()
}
``` [2](#0-1) 

At exactly `now_seconds == deadline`:
- `accepts_vote` returns **`false`** (voting is considered closed, because `now_seconds < deadline` is false)
- `register_vote`'s guard `now_seconds > deadline` is also **`false`**, so the vote is **not rejected**

The vote proceeds, is counted in the tally via `recompute_tally`, and `process_proposal` is called. Inside `process_proposal`, `can_make_decision` sees `expired = true` (because `accepts_vote` returns false) and, if a majority now exists, decides the proposal immediately. [3](#0-2) 

By contrast, the NNS governance `register_vote` correctly delegates to `accepts_vote` and is therefore consistent:

```rust
let accepts_vote = proposal.accepts_vote(now_seconds, voting_period_seconds);
if !accepts_vote {
    return Err(...);
}
``` [4](#0-3) 

Only the SNS governance path has this inconsistency.

### Impact Explanation
An unprivileged neuron holder can submit a `register_vote` ingress call timed to arrive in the exact IC round where `env.now() == deadline`. The vote is accepted and counted. If the neuron has sufficient voting power, this can:

1. **Flip a proposal outcome** — a proposal trending toward rejection can be adopted, causing arbitrary SNS governance actions (parameter changes, treasury transfers, canister upgrades) to execute that would otherwise have been blocked.
2. **Earn voting rewards** for a proposal that `accepts_vote` considers closed, creating an unfair reward advantage.

The impact is governance authorization bypass: a neuron exercises voting power at a moment the system's own `accepts_vote` predicate declares voting closed.

### Likelihood Explanation
IC rounds advance approximately every second. The attacker needs to submit their vote in the single round where `env.now() == deadline`. This is observable on-chain (the deadline is stored in `wait_for_quiet_state.current_deadline_timestamp_seconds`) and achievable by monitoring the subnet's block time and submitting the ingress message with appropriate timing. No privileged access, key material, or majority corruption is required — only a neuron with enough voting power to affect the tally and the ability to time an ingress call.

### Recommendation
Change the deadline check in `register_vote` from strict `>` to `>=`, making it consistent with `accepts_vote`:

```rust
// Before (allows vote at now_seconds == deadline):
if now_seconds > deadline {

// After (consistent with accepts_vote which uses now_seconds < deadline):
if now_seconds >= deadline {
``` [5](#0-4) 

This mirrors the fix recommended in the external report (changing `>` to `>=` in `beforeTicketRegistrationDeadline`) and aligns SNS governance with the NNS governance implementation which correctly uses `accepts_vote` as the single source of truth for deadline enforcement.

### Proof of Concept

1. Create an SNS proposal. Observe `deadline = proposal.get_deadline_timestamp_seconds()` from canister state.
2. Hold a neuron with enough voting power to flip the current tally.
3. Monitor the IC subnet's block time. In the round where `env.now() == deadline`, submit `manage_neuron { RegisterVote { proposal_id, vote: Yes } }`.
4. The call passes the `now_seconds > deadline` guard (false at equality), the vote is counted, `process_proposal` runs with `expired = true`, and if a majority now exists the proposal is adopted — even though `accepts_vote(deadline)` returns `false`.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1944-1968)
```rust
    pub fn process_proposal(&mut self, proposal_id: u64) {
        let now_seconds = self.env.now();

        let proposal_data = match self.proto.proposals.get_mut(&proposal_id) {
            None => return,
            Some(p) => p,
        };

        // Recompute the tally here. It should correctly reflect all votes until
        // the deadline, even after the proposal has been decided.
        if proposal_data.status() == ProposalDecisionStatus::Open
            || proposal_data.accepts_vote(now_seconds)
        {
            proposal_data.recompute_tally(now_seconds);
        }

        // If the status is open
        if proposal_data.status() != ProposalDecisionStatus::Open
            || !proposal_data.can_make_decision(now_seconds)
        {
            return;
        }

        // This marks the proposal_data as no longer open.
        proposal_data.decided_timestamp_seconds = now_seconds;
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
