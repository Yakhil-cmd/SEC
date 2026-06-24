### Title
Off-by-One Boundary Condition in NNS Governance `accepts_vote` Silently Rejects Votes at Exact Deadline - (File: rs/nns/governance/src/governance.rs)

---

### Summary

The NNS Governance `accepts_vote` function uses a strict `<` comparison against the proposal deadline, meaning a neuron vote submitted at the exact deadline second is silently rejected. This is the direct IC analog of the oToken boundary condition bug: a time-based gate uses a strict inequality that excludes the boundary moment, causing a valid action to fail at a well-defined, reachable instant.

---

### Finding Description

`ProposalData::accepts_vote` in `rs/nns/governance/src/governance.rs` is the single gate that controls whether a neuron may cast a vote on an open proposal:

```rust
pub fn accepts_vote(&self, now_seconds: u64, voting_period_seconds: u64) -> bool {
    now_seconds < self.get_deadline_timestamp_seconds(voting_period_seconds)
}
``` [1](#0-0) 

The strict `<` means the half-open interval `[proposal_creation, deadline)` is accepted, and the deadline second itself is excluded. At `now_seconds == deadline`, `accepts_vote` returns `false`, and `register_vote` immediately returns the error `"Proposal deadline has passed."`:

```rust
let accepts_vote = proposal.accepts_vote(now_seconds, voting_period_seconds);
if !accepts_vote {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Proposal deadline has passed.",
    ));
}
``` [2](#0-1) 

The SNS Governance counterpart uses the opposite (correct) strict inequality — `now_seconds > deadline` — so voting at the exact deadline second is permitted there:

```rust
let deadline = proposal.get_deadline_timestamp_seconds();
if now_seconds > deadline {
    return Err(GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Proposal deadline has passed.",
    ));
}
``` [3](#0-2) 

The existing test `test_no_voting_after_deadline` advances time to `deadline_seconds + 1`, deliberately skipping the exact boundary second and leaving the off-by-one undetected:

```rust
driver.advance_time_by(deadline_seconds + 1 - DEFAULT_TEST_START_TIMESTAMP_SECONDS);
``` [4](#0-3) 

The `evaluate_wait_for_quiet` function in NNS Governance uses `now_seconds > current_deadline` (strict `>`), so at `now_seconds == deadline` it would still evaluate WFQ — but because `accepts_vote` rejects the vote before `cast_vote_and_cascade_follow` is ever called, WFQ is never reached at the boundary second either:

```rust
|| now_seconds > current_deadline
``` [5](#0-4) 

---

### Impact Explanation

Any neuron whose `register_vote` ingress message is processed by the replica in the one-second window where `now_seconds == deadline` receives a `PreconditionFailed` error and its vote is permanently lost — the neuron cannot retry because time is monotonically increasing. If the rejected vote would have flipped the tally (e.g., a large neuron casting the deciding vote in a close governance proposal), the proposal outcome is incorrect. Because NNS governance controls protocol upgrades, subnet configuration, and treasury disbursements, a single incorrectly decided proposal can have protocol-wide consequences.

---

### Likelihood Explanation

The IC batch time advances in nanoseconds; `now_seconds` is the batch time divided by 10⁹. For any given proposal, there is exactly one second — the deadline second — during which this rejection occurs. For a standard 4-day voting period (345,600 seconds), the probability that a randomly timed vote lands in that window is approximately 1/345,600 ≈ 0.0003%. However, deadline seconds are predictable (the deadline is public on-chain), so an adversary who wants to suppress a specific neuron's vote can time a spam or resource-exhaustion attack to delay that neuron's transaction into the boundary second. The risk is therefore not purely accidental.

---

### Recommendation

Change the strict `<` to `<=` in `accepts_vote`:

```rust
pub fn accepts_vote(&self, now_seconds: u64, voting_period_seconds: u64) -> bool {
    now_seconds <= self.get_deadline_timestamp_seconds(voting_period_seconds)
}
```

This aligns NNS Governance with the SNS Governance behavior (`now_seconds > deadline` → reject) and makes the deadline second inclusive for voting, matching the documented semantics that the deadline is the last moment at which votes are accepted.

---

### Proof of Concept

1. A proposal is created at `T0` with `voting_period_seconds = V`. The deadline is `D = T0 + V`.
2. A neuron submits a `register_vote` ingress message timed so that the replica processes it at batch time `now_seconds = D`.
3. `accepts_vote(D, V)` evaluates `D < D` → `false`.
4. `register_vote` returns `Err(PreconditionFailed, "Proposal deadline has passed.")`.
5. The neuron's vote is silently dropped; the proposal proceeds without it.
6. The same vote submitted one second earlier (`now_seconds = D - 1`) would have been accepted, because `D - 1 < D` → `true`. [1](#0-0) [6](#0-5)

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

**File:** rs/nns/governance/tests/governance.rs (L1800-1800)
```rust
    driver.advance_time_by(deadline_seconds + 1 - DEFAULT_TEST_START_TIMESTAMP_SECONDS);
```
