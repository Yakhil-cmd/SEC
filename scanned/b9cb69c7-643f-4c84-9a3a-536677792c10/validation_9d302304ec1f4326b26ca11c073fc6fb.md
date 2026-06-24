### Title
SNS Governance `distribute_rewards` Uses `now` Instead of Round End Time to Collect Settled Proposals — (`rs/sns/governance/src/governance.rs`)

### Summary

In SNS governance, `distribute_rewards` collects proposals to settle by calling `ready_to_be_settled_proposal_ids()`, which internally uses `self.env.now()` (the actual wall-clock time of execution) rather than `reward_event_end_timestamp_seconds` (the nominal end of the reward round). Because `distribute_rewards` is always called some time after the round nominally ends, proposals whose voting deadlines fall in the gap between the round end and the actual execution time are incorrectly included in the current reward event instead of the next one. NNS governance avoids this exact bug by passing the round-end timestamp explicitly.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `distribute_rewards` first computes how many rounds have elapsed and derives `reward_event_end_timestamp_seconds`:

```rust
// line 5808-5814
let reward_start_timestamp_seconds = self
    .latest_reward_event()
    .end_timestamp_seconds
    .unwrap_or_default();
let new_rounds_count = now
    .saturating_sub(reward_start_timestamp_seconds)
    .saturating_div(round_duration_seconds);
...
// line 5837-5839
let reward_event_end_timestamp_seconds = new_rounds_count
    .saturating_mul(round_duration_seconds)
    .saturating_add(reward_start_timestamp_seconds);
``` [1](#0-0) 

However, the proposals to settle are collected **before** `reward_event_end_timestamp_seconds` is computed, and the collection uses `now` internally:

```rust
// line 5822-5823
let considered_proposals: Vec<ProposalId> =
    self.ready_to_be_settled_proposal_ids().collect();
``` [2](#0-1) 

`ready_to_be_settled_proposal_ids` captures `now` from the environment:

```rust
fn ready_to_be_settled_proposal_ids(&self) -> impl Iterator<Item = ProposalId> + '_ {
    let now = self.env.now();
    self.proto
        .proposals
        .iter()
        .filter(move |(_, data)| data.reward_status(now) == ProposalRewardStatus::ReadyToSettle)
        .map(|(k, _)| ProposalId { id: *k })
}
``` [3](#0-2) 

A proposal is `ReadyToSettle` when `now >= get_deadline_timestamp_seconds()`:

```rust
pub fn accepts_vote(&self, now_seconds: u64) -> bool {
    now_seconds < self.get_deadline_timestamp_seconds()
}
``` [4](#0-3) 

So any proposal whose voting deadline falls in the window `(reward_event_end_timestamp_seconds, now]` is included in the current reward event even though it should belong to the next one.

**NNS governance avoids this bug** by passing the round-end timestamp explicitly:

```rust
let as_of_timestamp_seconds =
    self.most_recent_fully_elapsed_reward_round_end_timestamp_seconds();
let considered_proposals: Vec<ProposalId> = self
    .ready_to_be_settled_proposal_ids(as_of_timestamp_seconds)
    .collect();
``` [5](#0-4) 

NNS's `ready_to_be_settled_proposal_ids` accepts the timestamp as a parameter:

```rust
pub fn ready_to_be_settled_proposal_ids(
    &self,
    as_of_timestamp_seconds: u64,
) -> impl Iterator<Item = ProposalId> + '_ {
``` [6](#0-5) 

### Impact Explanation

Proposals whose voting deadlines fall in the gap between `reward_event_end_timestamp_seconds` and `now` are settled in the current reward event. The reward purse for the current round (calculated correctly from token supply × rate × rounds) is then distributed across more proposals than intended. Neurons that voted on proposals legitimately belonging to the current round receive a diluted share of the purse. The affected proposals are marked `Settled` and will not appear in the next round, so their voters receive rewards from the wrong round's purse. This breaks the expected per-round reward accounting that SNS governance participants rely on.

### Likelihood Explanation

This occurs on every invocation of `distribute_rewards` because `run_periodic_tasks` (the timer) always fires some time after the round nominally ends. The window is typically seconds to minutes. Any proposal whose voting deadline (including wait-for-quiet extensions) falls in that window is affected. A participant who can predict when `distribute_rewards` will fire (based on the deterministic round schedule) can time a proposal's deadline to fall in the window, causing it to be settled in the current round rather than the next.

### Recommendation

Compute `reward_event_end_timestamp_seconds` before collecting proposals, then pass it to `ready_to_be_settled_proposal_ids` (refactored to accept a timestamp parameter, mirroring the NNS implementation):

```rust
let reward_event_end_timestamp_seconds = new_rounds_count
    .saturating_mul(round_duration_seconds)
    .saturating_add(reward_start_timestamp_seconds);

let considered_proposals: Vec<ProposalId> =
    self.ready_to_be_settled_proposal_ids(reward_event_end_timestamp_seconds).collect();
```

### Proof of Concept

1. SNS round duration = 7 days. Last round ended at `T`.
2. `distribute_rewards` fires at `T + 30 seconds` (i.e., `now = T + 30s`).
3. `reward_event_end_timestamp_seconds = T` (correct round boundary).
4. Proposal P has `get_deadline_timestamp_seconds() = T + 15s` (deadline fell 15 seconds after the round ended).
5. `ready_to_be_settled_proposal_ids()` evaluates `reward_status(now = T+30s)`: since `T+30s >= T+15s`, P is `ReadyToSettle` and is included in the current round.
6. Correct behavior: P should be evaluated against `T`, where `T < T+15s`, so P would still be `AcceptVotes` at the round boundary and belong to the next round.
7. Result: P is settled in the current round, diluting rewards for neurons that voted on proposals that legitimately belong to this round. [7](#0-6) [8](#0-7)

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

**File:** rs/sns/governance/src/governance.rs (L5763-5840)
```rust
    fn distribute_rewards(&mut self, supply: Tokens) {
        log!(INFO, "distribute_rewards. Supply: {:?}", supply);
        let now = self.env.now();

        // VotingRewardsParameters should always be set,
        // but we check and return early just in case.
        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            Some(voting_rewards_parameters) => voting_rewards_parameters,
            None => {
                log!(
                    ERROR,
                    "distribute_rewards called even though \
                     voting_rewards_parameters not set.",
                );
                return;
            }
        };

        let round_duration_seconds = match voting_rewards_parameters.round_duration_seconds {
            Some(s) => s,
            None => {
                log!(
                    ERROR,
                    "round_duration_seconds not set:\n{:#?}",
                    voting_rewards_parameters,
                );
                return;
            }
        };
        // This guard is needed, because we'll divide by this amount shortly.
        if round_duration_seconds == 0 {
            // This is important, but emitting this every time will be spammy, because this gets
            // called during run_periodic_tasks.
            log!(
                ERROR,
                "round_duration_seconds ({}) is not positive. \
                 Therefore, we cannot calculate voting rewards.",
                round_duration_seconds,
            );
            return;
        }

        let reward_start_timestamp_seconds = self
            .latest_reward_event()
            .end_timestamp_seconds
            .unwrap_or_default();
        let new_rounds_count = now
            .saturating_sub(reward_start_timestamp_seconds)
            .saturating_div(round_duration_seconds);
        if new_rounds_count == 0 {
            // This may happen, in case consider_distributing_rewards was called
            // several times at almost the same time. This is
            // harmless, just abandon.
            return;
        }

        let considered_proposals: Vec<ProposalId> =
            self.ready_to_be_settled_proposal_ids().collect();
        // RewardEvents are generated every time. If there are no proposals to reward, the rewards
        // purse is rolled over via the total_available_e8s_equivalent field.

        // Log if we are about to "backfill" rounds that were missed.
        if new_rounds_count > 1 {
            log!(
                INFO,
                "Some reward distribution should have happened, but were missed. \
                 It is now {}. Whereas, latest_reward_event:\n{:#?}",
                now,
                self.latest_reward_event(),
            );
        }
        let reward_event_end_timestamp_seconds = new_rounds_count
            .saturating_mul(round_duration_seconds)
            .saturating_add(reward_start_timestamp_seconds);

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

**File:** rs/nns/governance/src/governance.rs (L3661-3664)
```rust
    pub fn ready_to_be_settled_proposal_ids(
        &self,
        as_of_timestamp_seconds: u64,
    ) -> impl Iterator<Item = ProposalId> + '_ {
```

**File:** rs/nns/governance/src/governance.rs (L6658-6662)
```rust
        let as_of_timestamp_seconds =
            self.most_recent_fully_elapsed_reward_round_end_timestamp_seconds();
        let considered_proposals: Vec<ProposalId> = self
            .ready_to_be_settled_proposal_ids(as_of_timestamp_seconds)
            .collect();
```
