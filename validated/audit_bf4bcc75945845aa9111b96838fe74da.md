### Title
Off-by-One in SNS Governance `register_vote` Deadline Check Allows Voting at Exact Deadline Second, Enabling Unauthorized Wait-for-Quiet Extension - (File: rs/sns/governance/src/governance.rs)

---

### Summary

SNS governance's `register_vote` uses a strict `>` comparison (`now_seconds > deadline`) to gate votes, while the canonical `accepts_vote` helper uses strict `<` (`now_seconds < deadline`). At the exact second `now_seconds == deadline`, `register_vote` accepts the vote, but `accepts_vote` says the deadline has been reached. Because `evaluate_wait_for_quiet` also uses `>` (`now_seconds > current_deadline`), a vote cast at exactly the deadline second can still trigger a deadline extension via the wait-for-quiet algorithm.

---

### Finding Description

**Root cause — `register_vote` in SNS governance:** [1](#0-0) 

The guard is `now_seconds > deadline`. When `now_seconds == deadline` this condition is **false**, so the vote proceeds.

**Canonical deadline semantics — `accepts_vote` in SNS governance:** [2](#0-1) 

`accepts_vote` uses strict `<`, so at `now_seconds == deadline` it returns **false** — the deadline has been reached.

**Wait-for-quiet guard — `evaluate_wait_for_quiet`:** [3](#0-2) 

The early-return guard is also `now_seconds > current_deadline`. At `now_seconds == deadline` this is **false**, so the function proceeds to potentially extend the deadline.

**Contrast with NNS governance `register_vote`**, which correctly delegates to `accepts_vote` (strict `<`): [4](#0-3) 

NNS governance is consistent; SNS governance is not.

**Call chain after the vote is accepted:**

`register_vote` → `cast_vote_and_cascade_follow` → `process_proposal` → `recompute_tally` → `evaluate_wait_for_quiet` [5](#0-4) [6](#0-5) 

---

### Impact Explanation

An SNS neuron holder with enough voting power to flip the majority (yes→no or no→yes) can:

1. Observe the proposal's `current_deadline_timestamp_seconds` (public state).
2. Submit a vote in the IC round where `now_seconds == deadline`.
3. `register_vote` accepts the vote (off-by-one).
4. `evaluate_wait_for_quiet` does not skip (off-by-one), computes a `required_margin`, and extends `current_deadline_timestamp_seconds` by up to `wait_for_quiet_deadline_increase_seconds`.

The proposal's voting period is extended beyond its intended deadline. The maximum total extension is bounded by `2 * wait_for_quiet_deadline_increase_seconds` (typically 2 days for SNS defaults), but the attacker can exploit the boundary once per flip, delaying finalization of a governance proposal. This is a **governance authorization bug**: a vote that should be rejected at the deadline is accepted and mutates governance state.

---

### Likelihood Explanation

- The deadline is stored in public canister state and is readable by any observer.
- IC consensus produces blocks roughly every second; the attacker submits their ingress message targeting the round where `now_seconds == deadline`. This is a standard timing attack with no special privileges.
- The attacker only needs to control a neuron with sufficient voting power to flip the majority — a realistic condition for a large SNS token holder or a coordinated group.
- No mempool front-running is required; the attacker simply waits for the correct second and submits.

---

### Recommendation

Change the deadline guard in `register_vote` from strict `>` to `>=`, consistent with `accepts_vote`:

```diff
- if now_seconds > deadline {
+ if now_seconds >= deadline {
      return Err(GovernanceError::new_with_message(
          ErrorType::PreconditionFailed,
          "Proposal deadline has passed.",
      ));
  }
``` [7](#0-6) 

This makes `register_vote` consistent with `accepts_vote` and closes the one-second window in which a vote at exactly the deadline can trigger a wait-for-quiet extension.

---

### Proof of Concept

**Setup:** An SNS proposal with `initial_voting_period_seconds = 345600` (4 days) and `wait_for_quiet_deadline_increase_seconds = 86400` (1 day). The current tally slightly favors Yes. `current_deadline_timestamp_seconds = T`.

**Step 1 — Observe deadline:** Read `get_proposal` to obtain `T`.

**Step 2 — Wait:** Do nothing until the IC round where `env.now() == T`.

**Step 3 — Submit flip vote:** Call `manage_neuron { RegisterVote { proposal_id, vote: No } }` with a neuron large enough to flip the majority from Yes to No.

**Step 4 — Execution path:**
- `register_vote`: `now_seconds (T) > deadline (T)` → **false** → vote accepted.
- `cast_vote_and_cascade_follow` records the No vote, flipping the tally.
- `process_proposal` → `recompute_tally` → `evaluate_wait_for_quiet`:
  - `now_seconds (T) > current_deadline (T)` → **false** → does not return early.
  - `vote_has_turned` → **true** (Yes→No flip).
  - `required_margin = 86400 + 345600/2 - T_elapsed/2 > 0`.
  - `new_deadline = T + required_margin > T` → deadline extended.

**Result:** `current_deadline_timestamp_seconds` is now `T + ~86400+`, even though the deadline had been reached. The proposal cannot be finalized until the new deadline passes. [1](#0-0) [2](#0-1) [8](#0-7)

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

**File:** rs/sns/governance/src/proposal.rs (L2131-2139)
```rust
        let current_deadline = wait_for_quiet_state.current_deadline_timestamp_seconds;
        let deciding_amount_yes = new_tally.total / 2 + 1;
        let deciding_amount_no = new_tally.total.div_ceil(2);
        if new_tally.yes >= deciding_amount_yes
            || new_tally.no >= deciding_amount_no
            || now_seconds > current_deadline
        {
            return;
        }
```

**File:** rs/sns/governance/src/proposal.rs (L2241-2253)
```rust
        if let Some(old_tally) = self.latest_tally {
            if new_tally.yes == old_tally.yes
                && new_tally.no == old_tally.no
                && new_tally.total == old_tally.total
            {
                return;
            }

            self.evaluate_wait_for_quiet(now_seconds, &old_tally, &new_tally);
        }

        self.latest_tally = Some(new_tally);
    }
```

**File:** rs/nns/governance/src/governance.rs (L5629-5637)
```rust
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
