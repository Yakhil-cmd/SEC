### Title
SNS Governance Rewards Purse Permanently Lost When Proposals Settle With Zero Voting Participation — (`rs/sns/governance/src/governance.rs`)

---

### Summary

In `distribute_rewards`, when one or more proposals reach `ReadyToSettle` but no neuron cast an eligible vote on any of them, `total_reward_shares` is zero. The code skips maturity distribution but still marks those proposals as settled (writing a non-empty `settled_proposals` list into the new `RewardEvent`). Because the rollover gate `rewards_rolled_over()` only returns `true` when `settled_proposals.is_empty()`, the entire accumulated `rewards_purse_e8s` — including any amounts rolled over from previous rounds — is permanently discarded rather than carried forward.

---

### Finding Description

**Step 1 — Purse is computed (including prior rollovers)**

`distribute_rewards` builds `rewards_purse_e8s` by starting from `e8s_equivalent_to_be_rolled_over()` of the previous event and adding the current round's supply-fraction contribution. [1](#0-0) 

**Step 2 — Voting shares are summed; zero is possible**

`total_reward_shares` is the sum of `ballot.voting_power` for every ballot whose `Vote::eligible_for_rewards()` is true. If every ballot on every `ReadyToSettle` proposal is `Vote::Unspecified` (no neuron voted), the sum is exactly `dec!(0)`. [2](#0-1) 

**Step 3 — Zero-shares branch skips distribution but does NOT roll over**

When `total_reward_shares == dec!(0)`, the code logs a warning and leaves `distributed_e8s_equivalent = 0`. Execution then falls through to the proposal-settling loop and the final `RewardEvent` write. [3](#0-2) 

**Step 4 — Proposals are settled regardless**

All proposals in `considered_proposals` have their ballots cleared and `reward_event_end_timestamp_seconds` set, transitioning them to `Settled`. [4](#0-3) 

**Step 5 — RewardEvent is written with non-empty `settled_proposals`**

The new event records `settled_proposals = considered_proposals` (non-empty), `distributed_e8s_equivalent = 0`, and `total_available_e8s_equivalent = Some(rewards_purse_e8s)`. [5](#0-4) 

**Step 6 — Rollover gate is keyed only on `settled_proposals.is_empty()`**

`rewards_rolled_over()` returns `true` only when `settled_proposals` is empty. Because proposals were settled, it returns `false`. [6](#0-5) 

Consequently `e8s_equivalent_to_be_rolled_over()` returns `0`: [7](#0-6) 

The next call to `distribute_rewards` starts its purse from `0` rollover. The entire `rewards_purse_e8s` from the affected round is permanently lost.

---

### Impact Explanation

The rewards purse is proportional to `total_token_supply × reward_rate × elapsed_rounds`. For an SNS with a meaningful token supply and accumulated rollover from prior empty rounds, this can represent a large amount of maturity that should have been distributed to participating neurons but is instead silently discarded. The loss is irreversible: once the `RewardEvent` is committed with non-empty `settled_proposals`, no future round can recover the purse.

---

### Likelihood Explanation

This is reachable by any unprivileged user who can submit a proposal to an SNS:

1. Submit a proposal of a type that has no default followers configured (or where all neurons have disabled following for that action type).
2. The proposal's voting period expires with all ballots remaining `Unspecified`.
3. The proposal transitions to `ReadyToSettle`.
4. At the next reward round boundary, `distribute_rewards` runs, finds `total_reward_shares == 0`, and permanently discards the purse.

This is especially realistic for newly launched SNS DAOs with sparse neuron participation, or for proposal types (e.g., `GenericNervousSystemFunction` calls) that lack broad default-follower coverage. No privileged access is required; the attacker only needs to pay the proposal submission fee.

---

### Recommendation

When `total_reward_shares == dec!(0)` but `considered_proposals` is non-empty, the rewards purse must not be silently dropped. Two sound fixes:

1. **Treat zero-participation rounds as rollover events**: change `rewards_rolled_over()` to also return `true` when `distributed_e8s_equivalent == 0`, regardless of `settled_proposals`.
2. **Explicitly carry the purse forward**: when `total_reward_shares == 0`, do not settle the proposals in this round; leave them in `ReadyToSettle` so the next round can attempt distribution again (analogous to the judge's recommendation in the external report to separate shares accounting from proposal settlement).

The NNS governance has the same structural issue in `calculate_voting_rewards` when `total_voting_rights < 0.001`: [8](#0-7) [9](#0-8) 

---

### Proof of Concept

```
1. Deploy SNS with VotingRewardsParameters enabled and a non-zero reward rate.
2. Ensure no neuron has a default follower for ActionType X.
3. Submit a proposal of ActionType X from any neuron.
4. Allow the proposal's initial_voting_period_seconds to elapse without any neuron voting.
   → Proposal status: ReadyToSettle; all ballots: Unspecified.
5. Advance time past the reward round boundary.
   → distribute_rewards() is called.
   → total_reward_shares = dec!(0)  [no eligible votes]
   → distributed_e8s_equivalent = 0
   → settled_proposals = [proposal_id]  (non-empty)
   → RewardEvent written.
6. Advance time past the next reward round boundary.
   → distribute_rewards() called again.
   → e8s_equivalent_to_be_rolled_over() on previous event:
       rewards_rolled_over() → settled_proposals.is_empty() → false → returns 0
   → rewards_purse_e8s starts from 0 rollover.
   → The entire purse from step 5 is permanently lost.
```

### Citations

**File:** rs/sns/governance/src/governance.rs (L5854-5875)
```rust
        let rewards_purse_e8s = {
            let mut result = Decimal::from(
                self.latest_reward_event()
                    .e8s_equivalent_to_be_rolled_over(),
            );
            let supply = i2d(supply.get_e8s());

            for i in 1..=new_rounds_count {
                let seconds_since_genesis = round_duration_seconds
                    .saturating_mul(i)
                    .saturating_add(reward_start_timestamp_seconds)
                    .saturating_sub(self.proto.genesis_timestamp_seconds);

                let current_reward_rate = voting_rewards_parameters.reward_rate_at(
                    crate::reward::Instant::from_seconds_since_genesis(i2d(seconds_since_genesis)),
                );

                result += current_reward_rate * voting_rewards_parameters.round_duration() * supply;
            }

            result
        };
```

**File:** rs/sns/governance/src/governance.rs (L5892-5938)
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
        // Freeze reward shares, now that we are done adding them up.
        let neuron_id_to_reward_shares = neuron_id_to_reward_shares;
        let total_reward_shares: Decimal = neuron_id_to_reward_shares.values().sum();
        debug_assert!(
            total_reward_shares >= dec!(0),
            "total_reward_shares: {total_reward_shares} neuron_id_to_reward_shares: {neuron_id_to_reward_shares:#?}",
        );
```

**File:** rs/sns/governance/src/governance.rs (L5946-5952)
```rust
        if total_reward_shares == dec!(0) {
            log!(
                ERROR,
                "Warning: total_reward_shares is 0. Therefore, we skip increasing \
                 neuron maturity. neuron_id_to_reward_shares: {:#?}",
                neuron_id_to_reward_shares,
            );
```

**File:** rs/sns/governance/src/governance.rs (L6013-6081)
```rust
        for pid in &considered_proposals {
            // Before considering a proposal for reward, it must be fully processed --
            // because we're about to clear the ballots, so no further processing will be
            // possible.
            self.process_proposal(pid.id);

            let p = match self.get_proposal_data_mut(*pid) {
                Some(p) => p,
                None => {
                    log!(
                        ERROR,
                        "Cannot find proposal {}, despite it being considered for rewards distribution.",
                        pid.id
                    );
                    debug_assert!(
                        false,
                        "It appears that proposal {} has been deleted out from under us \
                         while we were distributing rewards. This should never happen. \
                         In production, this would be quietly swept under the rug and \
                         we would continue processing. Current state (Governance):\n{:#?}",
                        pid.id, self.proto,
                    );
                    continue;
                }
            };

            if p.status() == ProposalDecisionStatus::Open {
                log!(
                    ERROR,
                    "Proposal {} was considered for reward distribution despite \
                     being open. We will now force the proposal's status to be Rejected.",
                    pid.id
                );
                debug_assert!(
                    false,
                    "This should be unreachable. Current governance state:\n{:#?}",
                    self.proto,
                );

                // The next two statements put p into the Rejected status. Thus,
                // process_proposal will consider that it has nothing more to do
                // with the p.
                p.decided_timestamp_seconds = now;
                p.latest_tally = Some(Tally {
                    timestamp_seconds: now,
                    yes: 0,
                    no: 0,
                    total: 0,
                });
                debug_assert_eq!(
                    p.status(),
                    ProposalDecisionStatus::Rejected,
                    "Failed to force ProposalData status to become Rejected. p:\n{p:#?}",
                );
            }

            // This is where the proposal becomes Settled, at least in the eyes
            // of the ProposalData::reward_status method.
            p.reward_event_end_timestamp_seconds = Some(reward_event_end_timestamp_seconds);
            p.reward_event_round = new_reward_event_round;

            // Ballots are used to determine two things:
            //   1. (obviously and primarily) whether to execute the proposal.
            //   2. rewards
            // At this point, we no longer need ballots for either of these
            // things, and since they take up a fair amount of space, we take
            // this opportunity to jettison them.
            p.ballots.clear();
        }
```

**File:** rs/sns/governance/src/governance.rs (L6084-6092)
```rust
        self.proto.latest_reward_event = Some(RewardEvent {
            round: new_reward_event_round,
            actual_timestamp_seconds: now,
            settled_proposals: considered_proposals,
            distributed_e8s_equivalent,
            end_timestamp_seconds: Some(reward_event_end_timestamp_seconds),
            rounds_since_last_distribution: Some(new_rounds_count),
            total_available_e8s_equivalent,
        })
```

**File:** rs/sns/governance/src/types.rs (L2054-2060)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L2064-2067)
```rust
    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```

**File:** rs/nns/governance/src/governance.rs (L6712-6719)
```rust
        let reward_distribution = if total_voting_rights < 0.001 {
            println!(
                "{}WARNING: total_voting_rights == {}, even though considered_proposals \
                 is nonempty (see earlier log). Therefore, we skip incrementing maturity \
                 to avoid dividing by zero (or super small number).",
                LOG_PREFIX, total_voting_rights,
            );
            None
```

**File:** rs/nns/governance/src/reward/calculation.rs (L120-126)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }
```
