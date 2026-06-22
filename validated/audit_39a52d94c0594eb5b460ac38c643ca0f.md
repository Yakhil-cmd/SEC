### Title
SNS `distribute_rewards` Uses Live `now` Timestamp Instead of Round Boundary for Proposal Selection, Causing Incorrect Reward Round Assignment - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance `distribute_rewards` function selects proposals to settle using the live `now` timestamp, while the computed `reward_event_end_timestamp_seconds` (the official round boundary) is used only for marking proposals as settled. Any proposal whose voting deadline falls between the official round boundary and the actual call time is incorrectly included in the current reward round. The NNS governance explicitly guards against this by using a truncated round-boundary timestamp for proposal selection; SNS does not.

### Finding Description
In `rs/sns/governance/src/governance.rs`, `distribute_rewards` computes the official end of the current reward round:

```rust
let reward_event_end_timestamp_seconds = new_rounds_count
    .saturating_mul(round_duration_seconds)
    .saturating_add(reward_start_timestamp_seconds);
``` [1](#0-0) 

However, the proposals to settle are collected via `ready_to_be_settled_proposal_ids()`, which internally captures `self.env.now()` — the actual current time, not the round boundary:

```rust
fn ready_to_be_settled_proposal_ids(&self) -> impl Iterator<Item = ProposalId> + '_ {
    let now = self.env.now();
    self.proto.proposals.iter()
        .filter(move |(_, data)| data.reward_status(now) == ProposalRewardStatus::ReadyToSettle)
        .map(|(k, _)| ProposalId { id: *k })
}
``` [2](#0-1) 

A proposal's `reward_status` transitions to `ReadyToSettle` when `accepts_vote(now_seconds)` returns false, i.e., when `now_seconds >= get_deadline_timestamp_seconds()`: [3](#0-2) [4](#0-3) 

Since `now >= reward_event_end_timestamp_seconds` by construction (the function returns early if `new_rounds_count == 0`), any proposal whose deadline falls in the half-open interval `[reward_event_end_timestamp_seconds, now)` is included in the current round's distribution even though it belongs to the next round.

By contrast, the NNS governance explicitly uses a truncated round-boundary timestamp for proposal selection:

```rust
let as_of_timestamp_seconds =
    self.most_recent_fully_elapsed_reward_round_end_timestamp_seconds();
let considered_proposals: Vec<ProposalId> = self
    .ready_to_be_settled_proposal_ids(as_of_timestamp_seconds)
    .collect();
``` [5](#0-4) 

where `most_recent_fully_elapsed_reward_round_end_timestamp_seconds` floors `now` to the nearest completed round boundary: [6](#0-5) 

The NNS `ready_to_be_settled_proposal_ids` accepts an explicit `as_of_timestamp_seconds` parameter, while the SNS version does not: [7](#0-6) 

### Impact Explanation
Proposals whose voting deadlines fall between `reward_event_end_timestamp_seconds` and `now` are:

1. Included in the current round's reward purse distribution (they should be in the next round).
2. Marked as settled with `reward_event_end_timestamp_seconds` — the current round boundary — rather than the next round's boundary. [8](#0-7) 

This causes neurons that voted on those proposals to receive maturity from the current round's purse instead of the next round's, systematically misattributing rewards across rounds. While this does not enable double-claiming (each proposal is settled exactly once via `has_been_rewarded()`), it constitutes a governance accounting bug that affects every reward distribution event. [9](#0-8) 

### Likelihood Explanation
This condition is triggered on every invocation of `distribute_rewards` because `now` is always at least slightly greater than `reward_event_end_timestamp_seconds`. The window is typically small (seconds, bounded by heartbeat frequency), but it is systematic and deterministic. Any SNS proposal whose deadline falls within this window on any reward distribution cycle is affected.

### Recommendation
Refactor `ready_to_be_settled_proposal_ids` in SNS to accept an explicit cutoff timestamp (mirroring the NNS implementation), and pass `reward_event_end_timestamp_seconds` as the argument inside `distribute_rewards`:

```rust
// In distribute_rewards:
let considered_proposals: Vec<ProposalId> =
    self.ready_to_be_settled_proposal_ids_as_of(reward_event_end_timestamp_seconds)
        .collect();

// New method:
fn ready_to_be_settled_proposal_ids_as_of(
    &self,
    as_of_timestamp_seconds: u64,
) -> impl Iterator<Item = ProposalId> + '_ {
    self.proto.proposals.iter()
        .filter(move |(_, data)| {
            data.reward_status(as_of_timestamp_seconds) == ProposalRewardStatus::ReadyToSettle
        })
        .map(|(k, _)| ProposalId { id: *k })
}
```

This ensures only proposals that were `ReadyToSettle` at the official round boundary are included in the current round, matching the NNS behavior.

### Proof of Concept
1. SNS is configured with `round_duration_seconds = 86400`.
2. Last reward event ended at `T`; `reward_start_timestamp_seconds = T`.
3. At time `T + 86400 + 30`, `distribute_rewards` is called (30 s after the round boundary).
4. `reward_event_end_timestamp_seconds = T + 86400`.
5. `ready_to_be_settled_proposal_ids()` uses `now = T + 86400 + 30`.
6. A proposal with `get_deadline_timestamp_seconds() = T + 86400 + 5` satisfies `accepts_vote(T + 86400 + 30) = false`, so it is `ReadyToSettle` and included in the current round.
7. That proposal's deadline is after the round boundary; it belongs to the next round.
8. It is settled with `reward_event_end_timestamp_seconds = T + 86400` and neurons that voted on it receive maturity from the current round's purse — one full round early. [10](#0-9) [11](#0-10)

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

**File:** rs/sns/governance/src/governance.rs (L5808-5814)
```rust
        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
```

**File:** rs/sns/governance/src/governance.rs (L5822-5823)
```rust
        let considered_proposals: Vec<ProposalId> =
            self.ready_to_be_settled_proposal_ids().collect();
```

**File:** rs/sns/governance/src/governance.rs (L5837-5839)
```rust
        let reward_event_end_timestamp_seconds = new_rounds_count
            .saturating_mul(round_duration_seconds)
            .saturating_add(reward_start_timestamp_seconds);
```

**File:** rs/sns/governance/src/governance.rs (L6069-6072)
```rust
            // This is where the proposal becomes Settled, at least in the eyes
            // of the ProposalData::reward_status method.
            p.reward_event_end_timestamp_seconds = Some(reward_event_end_timestamp_seconds);
            p.reward_event_round = new_reward_event_round;
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

**File:** rs/sns/governance/src/proposal.rs (L2073-2075)
```rust
    pub fn has_been_rewarded(&self) -> bool {
        self.reward_event_end_timestamp_seconds.is_some() || self.reward_event_round > 0
    }
```

**File:** rs/sns/governance/src/proposal.rs (L2100-2103)
```rust
    pub fn accepts_vote(&self, now_seconds: u64) -> bool {
        // Checks if the proposal's deadline is still in the future.
        now_seconds < self.get_deadline_timestamp_seconds()
    }
```

**File:** rs/nns/governance/src/governance.rs (L3661-3677)
```rust
    pub fn ready_to_be_settled_proposal_ids(
        &self,
        as_of_timestamp_seconds: u64,
    ) -> impl Iterator<Item = ProposalId> + '_ {
        self.heap_data
            .proposals
            .iter()
            .filter(move |(_, proposal)| {
                let topic = proposal.topic();
                let voting_period_seconds = self.voting_period_seconds()(topic);
                let reward_status =
                    proposal.reward_status(as_of_timestamp_seconds, voting_period_seconds);

                reward_status == ProposalRewardStatus::ReadyToSettle
            })
            .map(|(k, _)| ProposalId { id: *k })
    }
```

**File:** rs/nns/governance/src/governance.rs (L3679-3696)
```rust
    /// Rounds now downwards to nearest multiple of REWARD_DISTRIBUTION_PERIOD_SECONDS after genesis
    fn most_recent_fully_elapsed_reward_round_end_timestamp_seconds(&self) -> u64 {
        let now = self.env.now();
        let genesis_timestamp_seconds = self.heap_data.genesis_timestamp_seconds;

        if genesis_timestamp_seconds > now {
            println!(
                "{}WARNING: genesis is in the future: {} vs. now = {})",
                LOG_PREFIX, genesis_timestamp_seconds, now,
            );
            return 0;
        }

        (now - genesis_timestamp_seconds) // Duration since genesis (in seconds).
            / REWARD_DISTRIBUTION_PERIOD_SECONDS // This is where the truncation happens. Whole number of rounds.
            * REWARD_DISTRIBUTION_PERIOD_SECONDS // Convert back into seconds.
            + self.heap_data.genesis_timestamp_seconds // Convert from duration to back to instant.
    }
```

**File:** rs/nns/governance/src/governance.rs (L6658-6662)
```rust
        let as_of_timestamp_seconds =
            self.most_recent_fully_elapsed_reward_round_end_timestamp_seconds();
        let considered_proposals: Vec<ProposalId> = self
            .ready_to_be_settled_proposal_ids(as_of_timestamp_seconds)
            .collect();
```
