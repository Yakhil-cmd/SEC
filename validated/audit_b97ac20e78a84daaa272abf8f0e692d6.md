Audit Report

## Title
SNS Governance `distribute_rewards` Settles Proposals Belonging to the Next Reward Round — (`File: rs/sns/governance/src/governance.rs`)

## Summary
SNS governance's `distribute_rewards` collects proposals to settle using `ready_to_be_settled_proposal_ids()`, which evaluates reward status against `now` (the current wall-clock time). The reward event it creates only covers the period up to `reward_event_end_timestamp_seconds`, which is strictly ≤ `now`. Any proposal whose voting deadline falls in the gap `(reward_event_end_timestamp_seconds, now]` is incorrectly swept into the current reward event, having its ballots permanently cleared and maturity distributed one full round early. The NNS governance avoids this exact issue by passing a round-boundary-truncated timestamp to its equivalent function.

## Finding Description

**Root cause — SNS `ready_to_be_settled_proposal_ids` uses `now` with no round-boundary cutoff.**

`ready_to_be_settled_proposal_ids` captures `now` internally and filters on `reward_status(now) == ReadyToSettle`: [1](#0-0) 

`distribute_rewards` calls this function with no timestamp argument, immediately before computing `reward_event_end_timestamp_seconds`: [2](#0-1) 

`reward_event_end_timestamp_seconds` is computed as the floor of elapsed time to the nearest completed round: [3](#0-2) 

This value is always ≤ `now`. The gap `now − reward_event_end_timestamp_seconds` can be anywhere from 0 to `round_duration_seconds − 1` seconds. Any proposal whose voting deadline falls in this gap satisfies `reward_status(now) == ReadyToSettle` but belongs to the **next** round. It is included in `considered_proposals` and settled under the current reward event.

Settlement permanently clears ballots: [4](#0-3) 

**NNS governance does this correctly.** Its `ready_to_be_settled_proposal_ids` accepts an explicit `as_of_timestamp_seconds` parameter: [5](#0-4) 

And `calculate_voting_rewards` passes the truncated round-end timestamp computed by `most_recent_fully_elapsed_reward_round_end_timestamp_seconds`: [6](#0-5) [7](#0-6) 

SNS governance has no equivalent guard.

## Impact Explanation

This is a **High** severity finding matching: *"Significant SNS security impact with concrete user or protocol harm."*

1. **Premature reward settlement.** Proposals that closed after the current reward round ended are settled in the current event. Maturity is credited to voting neurons immediately rather than in the next round.
2. **Reward dilution.** The rewards purse is sized for `new_rounds_count` rounds. Settling extra next-round proposals spreads that fixed purse across more voting-power shares, reducing per-neuron maturity for legitimate current-round voters.
3. **Disproportionate gain.** A neuron that is the sole or dominant voter on a proposal closing just after the round boundary receives full proportional reward one full round earlier than intended.
4. **Permanent ballot data loss.** Once settled, `p.ballots.clear()` is called irreversibly. Proposals settled one round early lose their ballot data permanently with no recourse. [8](#0-7) 

## Likelihood Explanation

This is a **deterministic, structural bug** that fires on every invocation of `distribute_rewards` whenever at least one proposal's voting deadline falls in the window `(reward_event_end_timestamp_seconds, now]`. Because `distribute_rewards` is triggered by a periodic timer (`should_distribute_rewards` fires as soon as `seconds_since_last_reward_event > round_duration_seconds`), and proposals can close at any second, this window is non-empty in virtually every real reward distribution cycle. [9](#0-8) 

No special privilege is required. Any SNS governance participant who can submit a proposal (or observe existing proposal deadlines) can time a proposal to close in this window.

## Recommendation

Mirror the NNS approach in SNS governance:

1. Add an `as_of_timestamp_seconds: u64` parameter to `ready_to_be_settled_proposal_ids` in `rs/sns/governance/src/governance.rs`.
2. Compute the round-boundary timestamp — i.e., `reward_event_end_timestamp_seconds` (already computed at line 5837) — before calling `ready_to_be_settled_proposal_ids`, and pass it as the cutoff.
3. This ensures only proposals whose voting period closed on or before the end of the most recently completed reward round are settled in the current event. [1](#0-0) 

## Proof of Concept

**Setup:**
- SNS with `round_duration_seconds = 604800` (7 days).
- Last reward event `end_timestamp_seconds = T₀`.
- Neuron A creates Proposal P with `initial_voting_period_seconds = 604801` at time `T₀ − 1`.
- Proposal P's deadline = `T₀ − 1 + 604801 = T₀ + 604800` (1 second past the next round boundary).

**Execution:**
1. At `T₀ + 604800 + 2`, the governance timer fires `should_distribute_rewards` → returns `true`.
2. `distribute_rewards` runs: `new_rounds_count = 1`, `reward_event_end_timestamp_seconds = T₀ + 604800`.
3. `ready_to_be_settled_proposal_ids()` evaluates `reward_status(now = T₀ + 604800 + 2)`: Proposal P's deadline `T₀ + 604800 < now` → `accepts_vote = false` → `ReadyToSettle`.
4. Proposal P is included in `considered_proposals` and settled under the current reward event with `end_timestamp_seconds = T₀ + 604800`.
5. Neuron A receives full proportional maturity for Proposal P in round 1, even though P closed 1 second into round 2.
6. In round 2, Proposal P is already `Settled` (`has_been_rewarded() == true`); no other neuron can earn rewards for it.

A deterministic integration test using PocketIC can reproduce this by setting `now` to `T₀ + 604800 + 2`, creating the proposal at `T₀ − 1`, and asserting that the resulting `RewardEvent` contains Proposal P despite its deadline being after `reward_event_end_timestamp_seconds`. [10](#0-9)

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

**File:** rs/sns/governance/src/governance.rs (L5735-5753)
```rust
        let seconds_since_last_reward_event = now.saturating_sub(
            self.latest_reward_event()
                .end_timestamp_seconds
                .unwrap_or_default(),
        );

        let round_duration_seconds = match voting_rewards_parameters.round_duration_seconds {
            Some(s) => s,
            None => {
                log!(
                    ERROR,
                    "round_duration_seconds unset:\n{:#?}",
                    voting_rewards_parameters,
                );
                return false;
            }
        };

        seconds_since_last_reward_event > round_duration_seconds
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

**File:** rs/sns/governance/src/governance.rs (L6071-6080)
```rust
            p.reward_event_end_timestamp_seconds = Some(reward_event_end_timestamp_seconds);
            p.reward_event_round = new_reward_event_round;

            // Ballots are used to determine two things:
            //   1. (obviously and primarily) whether to execute the proposal.
            //   2. rewards
            // At this point, we no longer need ballots for either of these
            // things, and since they take up a fair amount of space, we take
            // this opportunity to jettison them.
            p.ballots.clear();
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

**File:** rs/nns/governance/src/governance.rs (L6658-6662)
```rust
        let as_of_timestamp_seconds =
            self.most_recent_fully_elapsed_reward_round_end_timestamp_seconds();
        let considered_proposals: Vec<ProposalId> = self
            .ready_to_be_settled_proposal_ids(as_of_timestamp_seconds)
            .collect();
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
