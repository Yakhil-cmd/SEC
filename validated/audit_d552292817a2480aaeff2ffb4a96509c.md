### Title
Incomplete Upkeep Tracking Causes Decided-but-Still-Voting Proposals to Miss Periodic Tally Recomputation - (File: rs/sns/governance/src/governance.rs)

### Summary

In SNS governance's `process_proposals`, the `closest_proposal_deadline_timestamp_seconds` optimization only tracks `Open` proposals when updating the deadline cache, but the processing loop also handles decided proposals that still `accepts_vote`. When all proposals are decided (no `Open` proposals remain), the cache is set to `u64::MAX`, causing the early-return guard to fire on every subsequent heartbeat invocation and permanently skipping periodic tally recomputation for decided-but-still-accepting-votes proposals.

### Finding Description

`process_proposals` in `rs/sns/governance/src/governance.rs` contains a structural mismatch between two parts of the same function:

**Processing filter (line 2017–2019)** — includes both `Open` proposals and decided proposals that still `accepts_vote`:

```rust
.filter(|(_, info)| {
    info.status() == ProposalDecisionStatus::Open || info.accepts_vote(self.env.now())
})
```

**Deadline cache update (lines 2027–2043)** — only considers `Open` proposals:

```rust
self.closest_proposal_deadline_timestamp_seconds = self
    .proto.proposals.values()
    .filter(|data| data.status() == ProposalDecisionStatus::Open)
    ...
    .unwrap_or(u64::MAX);
``` [1](#0-0) 

When a proposal is decided early (e.g., by absolute majority) while its voting period is still open, it transitions out of `Open` status but continues to `accepts_vote`. After `process_proposals` runs once and processes it, the deadline cache is recomputed over only `Open` proposals. If no `Open` proposals remain, the cache is set to `u64::MAX`. The early-return guard:

```rust
if self.env.now() < self.closest_proposal_deadline_timestamp_seconds {
    return;
}
``` [2](#0-1) 

…fires on every subsequent `run_periodic_tasks` invocation, permanently preventing `process_proposals` from recomputing the tally for decided-but-still-accepting-votes proposals via the periodic path.

The `insert_proposal` function updates the cache only using `initial_voting_period_seconds` (not the wait-for-quiet extended deadline) and only when a new proposal is submitted: [3](#0-2) 

So if no new proposals are submitted, the cache stays at `u64::MAX` indefinitely.

The analog to the Solidity bug is exact: in the Solidity contract, `s_offersToUpkeep` only enrolled accepted offers, so pending offers were never checked for expiration. Here, `closest_proposal_deadline_timestamp_seconds` only enrolls `Open` proposals, so decided-but-still-accepting-votes proposals are never periodically processed.

### Impact Explanation

The periodic tally recomputation for decided-but-still-accepting-votes proposals is skipped. `process_proposal` for such a proposal calls `recompute_tally`, which updates `latest_tally` and potentially triggers `evaluate_wait_for_quiet`: [4](#0-3) [5](#0-4) 

The tally is used by `distribute_rewards` to determine voting reward shares. If `process_proposals` never runs for these proposals, the tally used at reward settlement time may be stale — reflecting only the state at the moment the proposal was decided, not votes cast afterward during the still-open voting period.

**Mitigating factor:** `register_vote` calls `process_proposal` directly after each vote cast: [6](#0-5) 

So the tally IS updated when neurons vote. The residual harm is that if no votes are cast after a proposal is decided, the tally is not recomputed by the periodic path. Additionally, `distribute_rewards` calls `process_proposal` before settling each proposal, but at that point `accepts_vote` is already false so `recompute_tally` is not invoked: [7](#0-6) 

The net impact is a governance correctness issue: the periodic tally update path is broken for decided-but-still-accepting-votes proposals when no `Open` proposals exist, which can cause the final tally used for reward distribution to not reflect the most recent state if no explicit votes were cast in the interim.

### Likelihood Explanation

This condition is triggered whenever a proposal reaches an absolute majority before its voting period ends and no other `Open` proposals exist — a common scenario in active SNS DAOs where a single high-participation proposal is voted in quickly. Any unprivileged neuron holder can trigger this by voting on a proposal, causing it to be decided early.

### Recommendation

Update the deadline cache to also track decided proposals that still `accepts_vote`, mirroring the processing filter:

```rust
self.closest_proposal_deadline_timestamp_seconds = self
    .proto.proposals.values()
    .filter(|data| {
        data.status() == ProposalDecisionStatus::Open
            || data.accepts_vote(self.env.now())
    })
    .map(|proposal_data| proposal_data.get_deadline_timestamp_seconds())
    .min()
    .unwrap_or(u64::MAX);
```

This ensures the cache tracks the earliest deadline among all proposals that still require periodic processing, matching the intent of the processing loop.

### Proof of Concept

1. SNS governance has no `Open` proposals.
2. Neuron A submits proposal P (voting period = 4 days).
3. Neurons with absolute majority immediately vote Yes; `process_proposal` is called via `register_vote`, P is decided, `decided_timestamp_seconds` is set.
4. `process_proposals` runs (called from `run_periodic_tasks`), processes P (recomputes tally), then recomputes `closest_proposal_deadline_timestamp_seconds` over only `Open` proposals → result is `u64::MAX`.
5. P is now `Decided` but `accepts_vote` returns `true` (voting period not yet over).
6. On every subsequent `run_periodic_tasks` invocation, `process_proposals` hits the early return `if self.env.now() < u64::MAX { return; }` and exits immediately.
7. P's tally is never recomputed by the periodic path for the remaining voting period.
8. Neurons that vote after step 3 do trigger `process_proposal` via `register_vote`, so their votes are reflected. But if no further votes are cast, the tally used at reward settlement is frozen at the state from step 3, and the periodic recomputation path is permanently broken until a new proposal is submitted. [8](#0-7) [9](#0-8) [3](#0-2)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1944-1965)
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
```

**File:** rs/sns/governance/src/governance.rs (L2006-2044)
```rust
    /// Processes all proposals with decision status ProposalStatusOpen
    pub fn process_proposals(&mut self) {
        if self.env.now() < self.closest_proposal_deadline_timestamp_seconds {
            // Nothing to do.
            return;
        }

        let pids = self
            .proto
            .proposals
            .iter()
            .filter(|(_, info)| {
                info.status() == ProposalDecisionStatus::Open || info.accepts_vote(self.env.now())
            })
            .map(|(pid, _)| *pid)
            .collect::<Vec<u64>>();

        for pid in pids {
            self.process_proposal(pid);
        }

        self.closest_proposal_deadline_timestamp_seconds = self
            .proto
            .proposals
            .values()
            .filter(|data| data.status() == ProposalDecisionStatus::Open)
            .map(|proposal_data| {
                proposal_data
                    .wait_for_quiet_state
                    .map(|w| w.current_deadline_timestamp_seconds)
                    .unwrap_or_else(|| {
                        proposal_data
                            .proposal_creation_timestamp_seconds
                            .saturating_add(proposal_data.initial_voting_period_seconds)
                    })
            })
            .min()
            .unwrap_or(u64::MAX);
    }
```

**File:** rs/sns/governance/src/governance.rs (L3397-3406)
```rust
    fn insert_proposal(&mut self, pid: u64, data: ProposalData) {
        let initial_voting_period_seconds = data.initial_voting_period_seconds;

        self.closest_proposal_deadline_timestamp_seconds = std::cmp::min(
            data.proposal_creation_timestamp_seconds + initial_voting_period_seconds,
            self.closest_proposal_deadline_timestamp_seconds,
        );
        self.proto.proposals.insert(pid, data);
        self.process_proposal(pid);
    }
```

**File:** rs/sns/governance/src/governance.rs (L3931-3946)
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

        Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L6013-6017)
```rust
        for pid in &considered_proposals {
            // Before considering a proposal for reward, it must be fully processed --
            // because we're about to clear the ballots, so no further processing will be
            // possible.
            self.process_proposal(pid.id);
```

**File:** rs/sns/governance/src/proposal.rs (L2100-2103)
```rust
    pub fn accepts_vote(&self, now_seconds: u64) -> bool {
        // Checks if the proposal's deadline is still in the future.
        now_seconds < self.get_deadline_timestamp_seconds()
    }
```

**File:** rs/sns/governance/src/proposal.rs (L2210-2253)
```rust
    pub fn recompute_tally(&mut self, now_seconds: u64) {
        // Tally proposal
        let mut yes = 0;
        let mut no = 0;
        let mut undecided = 0;
        for ballot in self.ballots.values() {
            let lhs: &mut u64 = if let Ok(vote) = Vote::try_from(ballot.vote) {
                match vote {
                    Vote::Unspecified => &mut undecided,
                    Vote::Yes => &mut yes,
                    Vote::No => &mut no,
                }
            } else {
                &mut undecided
            };
            *lhs = (*lhs).saturating_add(ballot.voting_power)
        }

        // It is validated in `make_proposal` that the total does not
        // exceed u64::MAX: the `saturating_add` is just a precaution.
        let total = yes.saturating_add(no).saturating_add(undecided);

        let new_tally = Tally {
            timestamp_seconds: now_seconds,
            yes,
            no,
            total,
        };

        // Every time the tally changes, (possibly) update the wait-for-quiet
        // dynamic deadline.
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
