### Title
SNS Governance `register_vote` Deadline Off-by-One Allows Voting at Exact Expiry Second While Proposal Is Simultaneously `ReadyToSettle` - (`rs/sns/governance/src/governance.rs`)

---

### Summary

In SNS governance, `register_vote` uses a strict-greater-than check (`now_seconds > deadline`) to reject late votes, while `accepts_vote` uses strict-less-than (`now_seconds < deadline`). At the exact second `now_seconds == deadline`, `register_vote` permits a vote to be cast, yet `accepts_vote` simultaneously returns `false`, causing `reward_status` to return `ReadyToSettle`. This creates a one-second boundary window where a neuron can cast a vote on a proposal that is simultaneously considered expired and eligible for reward settlement — an exact structural analog to M-30's epoch-boundary race.

---

### Finding Description

**Root cause — inconsistent boundary comparisons:**

`register_vote` in SNS governance rejects a vote only when `now_seconds > deadline`:

```rust
// rs/sns/governance/src/governance.rs, line 3916
let deadline = proposal.get_deadline_timestamp_seconds();
if now_seconds > deadline {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Proposal deadline has passed.",
    ));
}
``` [1](#0-0) 

`accepts_vote` uses the opposite strict bound — it returns `false` when `now_seconds == deadline`:

```rust
// rs/sns/governance/src/proposal.rs, line 2100-2102
pub fn accepts_vote(&self, now_seconds: u64) -> bool {
    now_seconds < self.get_deadline_timestamp_seconds()
}
``` [2](#0-1) 

`reward_status` delegates to `accepts_vote` to decide whether a proposal is `ReadyToSettle`:

```rust
// rs/sns/governance/src/proposal.rs, line 2043-2057
pub fn reward_status(&self, now_seconds: u64) -> ProposalRewardStatus {
    if self.has_been_rewarded() { return ProposalRewardStatus::Settled; }
    if self.accepts_vote(now_seconds) { return ProposalRewardStatus::AcceptVotes; }
    if self.is_eligible_for_rewards { ProposalRewardStatus::ReadyToSettle } ...
}
``` [3](#0-2) 

`distribute_rewards` collects all proposals whose `reward_status` is `ReadyToSettle` and settles them:

```rust
// rs/sns/governance/src/governance.rs, line 1927-1933
fn ready_to_be_settled_proposal_ids(&self) -> impl Iterator<Item = ProposalId> + '_ {
    let now = self.env.now();
    self.proto.proposals.iter()
        .filter(move |(_, data)| data.reward_status(now) == ProposalRewardStatus::ReadyToSettle)
        .map(|(k, _)| ProposalId { id: *k })
}
``` [4](#0-3) 

**Contrast with NNS governance**, which is consistent: `register_vote` calls `accepts_vote` directly and rejects when it returns `false`:

```rust
// rs/nns/governance/src/governance.rs, line 5630-5636
let accepts_vote = proposal.accepts_vote(now_seconds, voting_period_seconds);
if !accepts_vote {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed, "Proposal deadline has passed."));
}
``` [5](#0-4) 

NNS `accepts_vote` also uses `<`:

```rust
// rs/nns/governance/src/governance.rs, line 622
now_seconds < self.get_deadline_timestamp_seconds(voting_period_seconds)
``` [6](#0-5) 

SNS is the only governance implementation with the inconsistency.

**Additional compounding factor — wait-for-quiet at the boundary:**

`evaluate_wait_for_quiet` skips extension only when `now_seconds > current_deadline`:

```rust
// rs/sns/governance/src/proposal.rs, line 2136
|| now_seconds > current_deadline
``` [7](#0-6) 

At `now_seconds == deadline`, this guard does **not** fire. So a vote cast at the exact deadline second can flip the tally and trigger a wait-for-quiet deadline extension — extending the voting period — while `accepts_vote` simultaneously reports the proposal as expired and `ReadyToSettle`.

---

### Impact Explanation

At the exact second `now_seconds == deadline`, within the same IC consensus round:

1. **A neuron can cast a vote** via `register_vote` (the `>` check passes).
2. **The proposal is simultaneously `ReadyToSettle`** (because `accepts_vote` returns `false` via `<`).
3. **`distribute_rewards` can settle the proposal** in the same second, clearing ballots and distributing maturity.
4. **The vote may or may not be counted** depending on message ordering within the round — creating non-deterministic governance outcomes.
5. **A vote at the boundary can trigger wait-for-quiet extension** even though `accepts_vote` says the proposal is expired, potentially extending the voting period in a way that is inconsistent with the stated deadline semantics.

The governance correctness property violated: a proposal's voting period boundary should be unambiguous. The inconsistency means the boundary second is simultaneously "open for voting" (from `register_vote`'s perspective) and "closed for voting / ready to settle" (from `accepts_vote`'s perspective).

---

### Likelihood Explanation

IC consensus time (`env.now()`) advances in discrete one-second increments. Every proposal has a deterministic `deadline` value. Any neuron holder can observe the deadline from public state and submit a `register_vote` ingress message timed to arrive in the block where `now_seconds == deadline`. This requires no privileged access, no key compromise, and no majority corruption — only a standard ingress call from any neuron controller. The IC's deterministic block timing makes this boundary predictable.

---

### Recommendation

Change the deadline check in SNS `register_vote` from strict-greater-than to greater-than-or-equal, matching the semantics of `accepts_vote`:

```diff
- if now_seconds > deadline {
+ if now_seconds >= deadline {
      return Err(GovernanceError::new_with_message(
          ErrorType::PreconditionFailed,
          "Proposal deadline has passed.",
      ));
  }
```

Alternatively, rewrite the check to delegate to `accepts_vote` directly, as NNS does:

```rust
if !proposal.accepts_vote(now_seconds) {
    return Err(...);
}
```

Also audit `evaluate_wait_for_quiet`'s guard (`now_seconds > current_deadline`) for the same off-by-one: if the intent is that WFQ cannot fire once the deadline is reached, it should use `>=`.

---

### Proof of Concept

Given a proposal with `deadline = T` and `initial_voting_period_seconds = T` (so `proposal_creation_timestamp_seconds = 0`):

- At `now_seconds = T - 1`: `accepts_vote` returns `true`; `register_vote` allows the vote; `reward_status` = `AcceptVotes`. ✓ Consistent.
- At `now_seconds = T`: `accepts_vote` returns `false` (`T < T` is false); `reward_status` = `ReadyToSettle`; but `register_vote` allows the vote (`T > T` is false). ✗ **Inconsistent.**
- At `now_seconds = T + 1`: `accepts_vote` returns `false`; `register_vote` rejects (`T+1 > T`). ✓ Consistent.

The one-second window at `now_seconds == deadline` is the exact analog of M-30's `block.timestamp == lastEpochUpdate + EPOCH_DURATION` boundary, where both "advance epoch" and "exercise option" could execute simultaneously.

### Citations

**File:** rs/sns/governance/src/governance.rs (L1927-1934)
```rust
    fn ready_to_be_settled_proposal_ids(&self) -> impl Iterator<Item = ProposalId> + '_ {
        let now = self.env.now();
        self.proto
            .proposals
            .iter()
            .filter(move |(_, data)| data.reward_status(now) == ProposalRewardStatus::ReadyToSettle)
            .map(|(k, _)| ProposalId { id: *k })
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

**File:** rs/sns/governance/src/proposal.rs (L2043-2058)
```rust
    pub fn reward_status(&self, now_seconds: u64) -> ProposalRewardStatus {
        if self.has_been_rewarded() {
            return ProposalRewardStatus::Settled;
        }

        if self.accepts_vote(now_seconds) {
            return ProposalRewardStatus::AcceptVotes;
        }

        // TODO(NNS1-2731): Replace this with just ReadyToSettle.
        if self.is_eligible_for_rewards {
            ProposalRewardStatus::ReadyToSettle
        } else {
            ProposalRewardStatus::Settled
        }
    }
```

**File:** rs/sns/governance/src/proposal.rs (L2100-2103)
```rust
    pub fn accepts_vote(&self, now_seconds: u64) -> bool {
        // Checks if the proposal's deadline is still in the future.
        now_seconds < self.get_deadline_timestamp_seconds()
    }
```

**File:** rs/sns/governance/src/proposal.rs (L2134-2139)
```rust
        if new_tally.yes >= deciding_amount_yes
            || new_tally.no >= deciding_amount_no
            || now_seconds > current_deadline
        {
            return;
        }
```

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

**File:** rs/nns/governance/src/governance.rs (L5630-5637)
```rust
        let accepts_vote = proposal.accepts_vote(now_seconds, voting_period_seconds);
        if !accepts_vote {
            // Deadline has passed, so the proposal cannot be voted on
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Proposal deadline has passed.",
            ));
        }
```
