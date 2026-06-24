### Title
SNS Governance `distribute_rewards` Settles Proposals Belonging to the Next Reward Round — (`File: rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance `distribute_rewards` function collects proposals to settle using `ready_to_be_settled_proposal_ids()`, which evaluates proposal reward status against `now` (the current wall-clock time). However, the reward event it creates only covers the period up to `reward_event_end_timestamp_seconds`, which is strictly ≤ `now`. Any proposal whose voting period closed in the gap between `reward_event_end_timestamp_seconds` and `now` — i.e., a proposal that belongs to the **next** reward round — is incorrectly settled in the current reward event. The NNS governance avoids this exact bug by passing a past-truncated timestamp (`most_recent_fully_elapsed_reward_round_end_timestamp_seconds`) to its equivalent function.

---

### Finding Description

**SNS governance — missing round-boundary timestamp in proposal collection**

In `rs/sns/governance/src/governance.rs`, `ready_to_be_settled_proposal_ids()` captures `now` internally and filters proposals whose `reward_status(now) == ReadyToSettle`: [1](#0-0) 

`distribute_rewards` calls this function without any timestamp argument: [2](#0-1) 

Immediately after, it computes `reward_event_end_timestamp_seconds` as the end of the most recently **completed** round — a value that is always ≤ `now`: [3](#0-2) 

The gap `now − reward_event_end_timestamp_seconds` equals `(now − reward_start) mod round_duration_seconds`, which can be anywhere from 0 to `round_duration_seconds − 1` seconds. Any proposal whose voting deadline fell inside this gap is `ReadyToSettle` as of `now` but belongs to the **next** round. It is swept into `considered_proposals` and settled — with its ballots cleared and maturity distributed — under the current reward event, whose `end_timestamp_seconds` predates the proposal's close time.

**NNS governance does this correctly.** Its `ready_to_be_settled_proposal_ids` accepts an explicit `as_of_timestamp_seconds` parameter: [4](#0-3) 

And `calculate_voting_rewards` passes the truncated round-end timestamp: [5](#0-4) 

That truncated timestamp is computed by `most_recent_fully_elapsed_reward_round_end_timestamp_seconds`, which floors `now` to the nearest completed-round boundary: [6](#0-5) 

SNS governance has no equivalent guard.

---

### Impact Explanation

1. **Premature reward settlement.** Proposals that closed after the current reward round ended are settled in the current event. Their ballots are cleared immediately, and maturity is credited to voting neurons now, rather than in the next round.

2. **Reward dilution for current-round voters.** The rewards purse for the current event is sized for `new_rounds_count` rounds. Settling extra proposals from the next round spreads that fixed purse across more voting-power shares, reducing per-neuron maturity for neurons that voted on legitimately current-round proposals.

3. **Disproportionate gain for early-round voters.** A neuron that is the sole or dominant voter on a proposal that closes just after the round boundary receives the full proportional reward for that proposal one full round earlier than intended, before other participants have had a chance to vote on it in its proper round.

4. **Ballot data loss.** Once settled, a proposal's ballots are cleared (`std::mem::take` / equivalent). Proposals settled one round early lose their ballot data permanently, with no recourse. [7](#0-6) 

---

### Likelihood Explanation

This is a **deterministic, structural bug** that fires on every invocation of `distribute_rewards` whenever at least one proposal's voting deadline falls in the window `(reward_event_end_timestamp_seconds, now]`. Because `distribute_rewards` is triggered by a periodic timer task (`should_distribute_rewards` fires as soon as `seconds_since_last_reward_event > round_duration_seconds`), and proposals can close at any second, this window is non-empty in virtually every real reward distribution cycle. [8](#0-7) 

No special privilege is required. Any SNS governance participant who can submit a proposal (or observe existing proposal deadlines) can time a proposal to close in this window.

---

### Recommendation

Mirror the NNS approach in SNS governance:

1. Add an `as_of_timestamp_seconds: u64` parameter to `ready_to_be_settled_proposal_ids` in `rs/sns/governance/src/governance.rs`.
2. Compute the round-boundary timestamp analogous to NNS `most_recent_fully_elapsed_reward_round_end_timestamp_seconds` — i.e., `reward_event_end_timestamp_seconds` (already computed at line 5837) — and pass it to `ready_to_be_settled_proposal_ids` instead of using `now`.
3. This ensures only proposals whose voting period closed on or before the end of the most recently completed reward round are settled in the current event.

---

### Proof of Concept

**Setup:**
- SNS with `round_duration_seconds = 604800` (7 days).
- Last reward event `end_timestamp_seconds = T₀`.
- Neuron A creates Proposal P with `initial_voting_period_seconds = 604801` (7 days + 1 second) at time `T₀ − 1`.
- Proposal P's deadline = `T₀ − 1 + 604801 = T₀ + 604800` = exactly 1 second after the next round boundary.

**Execution:**
1. At `T₀ + 604800 + 2` (2 seconds after the round boundary), the governance timer fires `should_distribute_rewards` → returns `true` (elapsed > `round_duration_seconds`).
2. `distribute_rewards` runs:
   - `new_rounds_count = floor(604802 / 604800) = 1`
   - `reward_event_end_timestamp_seconds = T₀ + 604800` (the round boundary, 2 seconds in the past)
3. `ready_to_be_settled_proposal_ids()` evaluates `reward_status(now = T₀ + 604800 + 2)`:
   - Proposal P's deadline = `T₀ + 604800` < `now` → `accepts_vote` = false → status = `ReadyToSettle`.
4. Proposal P is included in `considered_proposals` and settled under the current reward event whose `end_timestamp_seconds = T₀ + 604800`.
5. Neuron A (sole voter) receives full proportional maturity for Proposal P in round 1, even though P closed 1 second into round 2.
6. In round 2, Proposal P is already `Settled`; no other neuron can earn rewards for it. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1927-1933)
```rust
    fn ready_to_be_settled_proposal_ids(&self) -> impl Iterator<Item = ProposalId> + '_ {
        let now = self.env.now();
        self.proto
            .proposals
            .iter()
            .filter(move |(_, data)| data.reward_status(now) == ProposalRewardStatus::ReadyToSettle)
            .map(|(k, _)| ProposalId { id: *k })
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

**File:** rs/sns/governance/src/governance.rs (L5808-5823)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L5837-5839)
```rust
        let reward_event_end_timestamp_seconds = new_rounds_count
            .saturating_mul(round_duration_seconds)
            .saturating_add(reward_start_timestamp_seconds);
```

**File:** rs/sns/governance/src/governance.rs (L5892-5931)
```rust
        // Add up reward shares based on voting power that was exercised.
        let mut neuron_id_to_reward_shares: HashMap<NeuronId, Decimal> = HashMap::new();
        for proposal_id in &considered_proposals {
            if let Some(proposal) = self.get_proposal_data(*proposal_id) {
                for (voter, ballot) in &proposal.ballots {
                    #[allow(clippy::blocks_in_conditions)]
                    if !Vote::try_from(ballot.vote)
                        .unwrap_or_else(|_| {
                            println!(
                                "{}Vote::from invoked with unexpected value {}.",
                                log_prefix(),
                                ballot.vote
                            );
                            Vote::Unspecified
                        })
                        .eligible_for_rewards()
                    {
                        continue;
                    }

                    match NeuronId::from_str(voter) {
                        Ok(neuron_id) => {
                            let reward_shares = i2d(ballot.voting_power);
                            *neuron_id_to_reward_shares
                                .entry(neuron_id)
                                .or_insert_with(|| dec!(0)) += reward_shares;
                        }
                        Err(e) => {
                            log!(
                                ERROR,
                                "Could not use voter {} to calculate total_voting_rights \
                                 since it's NeuronId was invalid. Underlying error: {:?}.",
                                voter,
                                e
                            );
                        }
                    }
                }
            }
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
