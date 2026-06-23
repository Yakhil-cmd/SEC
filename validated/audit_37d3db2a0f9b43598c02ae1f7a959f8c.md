### Title
SNS Governance Reward Distribution Uses `now` Instead of Round-End Timestamp as Proposal Cutoff, Causing Cross-Round Settlement Misattribution - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `distribute_rewards` function settles proposals using `now` (the actual wall-clock time of the distribution call) as the cutoff for `ready_to_be_settled_proposal_ids`, rather than the nominal end timestamp of the reward round being settled. This causes proposals whose voting periods ended in the gap between the round's nominal end and the actual distribution call to be incorrectly settled in the current reward event instead of the next one. The NNS governance canister correctly uses `most_recent_fully_elapsed_reward_round_end_timestamp_seconds()` as the cutoff and does not have this bug.

---

### Finding Description

**Root cause in SNS governance:**

`ready_to_be_settled_proposal_ids` in the SNS captures `self.env.now()` internally and uses it as the cutoff:

```rust
// rs/sns/governance/src/governance.rs:1927-1934
fn ready_to_be_settled_proposal_ids(&self) -> impl Iterator<Item = ProposalId> + '_ {
    let now = self.env.now();
    self.proto.proposals.iter()
        .filter(move |(_, data)| data.reward_status(now) == ProposalRewardStatus::ReadyToSettle)
        .map(|(k, _)| ProposalId { id: *k })
}
``` [1](#0-0) 

A proposal's `reward_status(now_seconds)` returns `ReadyToSettle` when `now_seconds >= deadline`, i.e., when the voting period has ended: [2](#0-1) 

Inside `distribute_rewards`, the nominal end of the reward round is computed as:

```rust
let reward_event_end_timestamp_seconds = new_rounds_count
    .saturating_mul(round_duration_seconds)
    .saturating_add(reward_start_timestamp_seconds);
``` [3](#0-2) 

This `reward_event_end_timestamp_seconds` is strictly in the past relative to `now` (since `should_distribute_rewards` only fires when `now > last_event.end + round_duration`). The proposals settled are those where `now >= deadline`, but the reward purse is calculated only for the period up to `reward_event_end_timestamp_seconds`. Any proposal whose voting deadline falls in the gap `(reward_event_end_timestamp_seconds, now]` is settled using the current round's reward purse even though it belongs to the next round. [4](#0-3) 

**Contrast with NNS governance (correct behavior):**

The NNS `ready_to_be_settled_proposal_ids` accepts an explicit `as_of_timestamp_seconds` parameter: [5](#0-4) 

In `calculate_voting_rewards`, the NNS passes `most_recent_fully_elapsed_reward_round_end_timestamp_seconds()` as the cutoff, which floors `now` to the nearest completed round boundary: [6](#0-5) [7](#0-6) 

This ensures the NNS only settles proposals that became ready before the round nominally ended, not proposals that became ready in the gap between the round end and the actual distribution call.

**Call path for SNS:**

`run_periodic_tasks` → `should_distribute_rewards` → `ledger.total_supply().await` → `distribute_rewards(supply)` → `ready_to_be_settled_proposal_ids()` (uses `now`). [8](#0-7) 

The `ledger.total_supply().await` inter-canister call introduces a non-deterministic delay between when `should_distribute_rewards` returns `true` and when `distribute_rewards` actually runs, widening the gap window.

---

### Impact Explanation

Proposals whose voting periods end in the gap between the nominal round end and the actual distribution call are settled in the wrong reward event. Concretely:

- The reward purse for round N is distributed among proposals that should belong to round N+1.
- Neurons that voted on those "gap" proposals receive their maturity increase in round N instead of round N+1, distorting the intended reward schedule.
- The round N+1 reward event will have fewer proposals to settle (those gap proposals are already gone), so its reward purse rolls over or is distributed among fewer voters, again deviating from the intended outcome.
- This matches the external report's impact class: **manipulation of governance voting result deviating from voted outcome** (wrong proposals settled in wrong round) and **contract fails to deliver promised returns** (neurons rewarded in the wrong round, reward purse misattributed).

No funds are lost outright, but the reward distribution deviates systematically from the intended per-round accounting.

---

### Likelihood Explanation

The gap between `reward_event_end_timestamp_seconds` and `now` is always non-zero in practice because:

1. `run_periodic_tasks` is called on a timer, not exactly at the round boundary.
2. The `ledger.total_supply().await` inter-canister call adds additional latency before `distribute_rewards` executes.
3. Any SNS with an active proposal whose voting period ends near a round boundary will trigger this condition in normal operation, without any attacker action.

This is a systematic, always-present ordering bug, not a race condition requiring special timing.

---

### Recommendation

Change `ready_to_be_settled_proposal_ids` in the SNS to accept an explicit cutoff timestamp (mirroring the NNS design), and pass `reward_event_end_timestamp_seconds` (the nominal round end) as the cutoff inside `distribute_rewards`:

```rust
// In distribute_rewards, after computing reward_event_end_timestamp_seconds:
let considered_proposals: Vec<ProposalId> =
    self.ready_to_be_settled_proposal_ids(reward_event_end_timestamp_seconds).collect();
```

And update the function signature:

```rust
fn ready_to_be_settled_proposal_ids(&self, as_of_timestamp_seconds: u64)
    -> impl Iterator<Item = ProposalId> + '_
{
    self.proto.proposals.iter()
        .filter(move |(_, data)|
            data.reward_status(as_of_timestamp_seconds) == ProposalRewardStatus::ReadyToSettle)
        .map(|(k, _)| ProposalId { id: *k })
}
```

This matches the NNS implementation exactly and ensures proposals are settled in the correct reward round.

---

### Proof of Concept

Scenario (no privileged access required):

1. SNS is initialized. Round duration = 7 days. Last reward event ended at `T0`.
2. A proposal P is submitted. Its voting period ends at `T0 + 7 days + 30 seconds` (30 seconds after the nominal start of round N+1).
3. `run_periodic_tasks` fires at `T0 + 7 days + 60 seconds` (60 seconds into round N+1). `should_distribute_rewards()` returns `true` because `now > T0 + 7 days`.
4. `ledger.total_supply().await` completes. `now` is now `T0 + 7 days + 65 seconds`.
5. `distribute_rewards` computes `reward_event_end_timestamp_seconds = T0 + 7 days` (round N end).
6. `ready_to_be_settled_proposal_ids()` uses `now = T0 + 7 days + 65 seconds`. Proposal P has deadline `T0 + 7 days + 30 seconds < now`, so it is `ReadyToSettle` and is included.
7. Proposal P is settled in round N's reward event, consuming round N's reward purse, even though it should be settled in round N+1.
8. Round N+1's reward event will not include P (already settled), so its purse is distributed among fewer proposals or rolls over entirely.

The attacker-controlled entry path is simply submitting a proposal to any SNS — an action available to any token holder — and the bug triggers automatically at the next reward distribution boundary.

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

**File:** rs/sns/governance/src/governance.rs (L5503-5521)
```rust
        let should_distribute_rewards = self.should_distribute_rewards();

        // Getting the total governance token supply from the ledger is expensive enough
        // that we don't want to do it on every call to `run_periodic_tasks`. So
        // we only fetch it when it's needed, which is when rewards should be
        // distributed
        if should_distribute_rewards {
            match self.ledger.total_supply().await {
                Ok(supply) => {
                    // Distribute rewards
                    self.distribute_rewards(supply);
                }
                Err(e) => log!(
                    ERROR,
                    "Error when getting total governance token supply: {}",
                    GovernanceError::from(e)
                ),
            }
        }
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

**File:** rs/nns/governance/src/governance.rs (L3658-3677)
```rust
    }

    // This is slow, because it scans all proposals.
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

**File:** rs/nns/governance/src/governance.rs (L3680-3696)
```rust
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
