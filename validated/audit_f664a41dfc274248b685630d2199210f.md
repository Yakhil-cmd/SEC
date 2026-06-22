### Title
Off-by-One in SNS Governance Voting Deadline Allows Vote at Exact Deadline Second - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The `register_vote` function in SNS governance uses a strictly-greater-than comparison (`now_seconds > deadline`) to gate vote acceptance, while the canonical `accepts_vote` helper uses strictly-less-than (`now_seconds < deadline`). This off-by-one means a neuron holder can cast a vote at exactly `now_seconds == deadline` — a timestamp at which the voting period has officially ended per the protocol's own definition — and have that vote recorded and counted.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, `register_vote` performs its deadline check as:

```rust
let deadline = proposal.get_deadline_timestamp_seconds();
if now_seconds > deadline {
    return Err(...);
}
``` [1](#0-0) 

The protocol's canonical definition of "still accepting votes" is expressed in `accepts_vote` in `rs/sns/governance/src/proposal.rs`:

```rust
pub fn accepts_vote(&self, now_seconds: u64) -> bool {
    now_seconds < self.get_deadline_timestamp_seconds()
}
``` [2](#0-1) 

The two conditions are **not equivalent**:

| `now_seconds` vs `deadline` | `register_vote` allows vote? | `accepts_vote` returns? |
|---|---|---|
| `now_seconds < deadline` | Yes | `true` |
| `now_seconds == deadline` | **Yes (bug)** | **`false`** |
| `now_seconds > deadline` | No | `false` |

When `now_seconds == deadline`, `register_vote` does not return an error, proceeds past the guard, and calls `cast_vote_and_cascade_follow`, permanently recording the ballot. [3](#0-2) 

By contrast, the NNS governance `register_vote` correctly delegates to `accepts_vote` (which uses `<`), so it rejects votes at exactly the deadline:

```rust
let accepts_vote = proposal.accepts_vote(now_seconds, voting_period_seconds);
if !accepts_vote {
    return Err(...);
}
``` [4](#0-3) 

---

### Impact Explanation

A neuron holder in any SNS can submit a `manage_neuron::RegisterVote` ingress message timed to arrive at exactly the deadline second. At that moment:

1. The vote is accepted and permanently written into `proposal.ballots` via `cast_vote_and_cascade_follow`.
2. `process_proposal` is called, potentially deciding the proposal with the late vote included.
3. `accepts_vote` simultaneously returns `false` for that same timestamp, meaning `reward_status` classifies the proposal as `ReadyToSettle` rather than `AcceptVotes` — the late voter's ballot is counted in the tally but the voter may not receive voting rewards, creating an asymmetric incentive for a large token holder to swing a close vote without cost.

The most dangerous scenario: a neuron holding a decisive stake observes the live tally (tallies are publicly readable via query calls), waits until `now_seconds == deadline`, and casts a vote that flips the outcome — after all other participants believe the window has closed.

---

### Likelihood Explanation

- The Internet Computer's consensus layer advances time in discrete rounds; `now_seconds` is the Unix timestamp of the current block. An attacker can monitor the on-chain time and submit their ingress message in the round where `now_seconds` equals the stored `current_deadline_timestamp_seconds`.
- No privileged access is required. Any principal controlling a neuron with a ballot on the proposal can exploit this.
- The attack is deterministic and repeatable across every SNS deployment.

---

### Recommendation

Replace the inline `>` comparison in `register_vote` with the canonical `accepts_vote` helper, matching the NNS pattern:

```rust
// Before (buggy):
if now_seconds > deadline {

// After (correct):
if !proposal.accepts_vote(now_seconds) {
``` [1](#0-0) 

This ensures a single, consistent definition of "voting period open" is used everywhere in the SNS governance canister.

---

### Proof of Concept

1. An SNS proposal is created with `current_deadline_timestamp_seconds = T`.
2. At time `T − 1`, the tally shows the vote is close (e.g., 50.1% Yes).
3. The attacker, holding a large neuron that voted No, waits.
4. At time `T` (exactly the deadline), the attacker submits `manage_neuron { RegisterVote { proposal_id, vote: No } }`.
5. `register_vote` evaluates `now_seconds (T) > deadline (T)` → `false` → no error.
6. `cast_vote_and_cascade_follow` records the No vote; `process_proposal` is called and the proposal is decided as Rejected.
7. Any other neuron holder attempting to vote at time `T` would face the same window — but the attacker had full knowledge of the tally before casting, while earlier voters did not. [5](#0-4) [2](#0-1)

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

**File:** rs/sns/governance/src/governance.rs (L3931-3942)
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
