### Title
SNS Governance Rewards Purse Permanently Lost When `total_reward_shares` Is Zero With Settled Proposals - (File: rs/sns/governance/src/governance.rs)

### Summary

In `distribute_rewards` within the SNS governance canister, when `total_reward_shares == 0` (no neuron voted on any considered proposal) but there are proposals ready to settle, the entire accumulated `rewards_purse_e8s` is permanently destroyed rather than rolled over to the next reward event. This is the direct IC analog of M-16: a denominator (total shares) reaching zero while pending value exists, causing that value to be irrecoverably lost.

### Finding Description

`distribute_rewards` computes a `rewards_purse_e8s` from the token supply and the reward rate, accumulating any previously rolled-over amounts. It then builds `neuron_id_to_reward_shares` from the ballots of `considered_proposals`. If no neuron voted on any of those proposals, `total_reward_shares` is `Decimal::ZERO`. [1](#0-0) 

When this branch is taken, the distribution loop is skipped entirely. However, the code continues to settle every proposal in `considered_proposals`: [2](#0-1) 

Each settled proposal has `reward_event_end_timestamp_seconds` set and its ballots cleared. The resulting `RewardEvent` therefore has a non-empty `settled_proposals` list. The roll-over logic in `RewardEvent::rewards_rolled_over` returns `true` only when `settled_proposals.is_empty()`: [3](#0-2) 

Because `settled_proposals` is not empty, `e8s_equivalent_to_be_rolled_over` returns `0`: [4](#0-3) 

The next call to `distribute_rewards` therefore starts with a fresh purse, and the entire `rewards_purse_e8s` from the current round is silently discarded. The NNS governance exhibits the identical pattern under its `total_voting_rights < 0.001` guard: [5](#0-4) [6](#0-5) 

### Impact Explanation

Any ICP-equivalent maturity that was accrued in `rewards_purse_e8s` for the affected round is permanently destroyed — it is neither distributed to neurons nor carried forward. For an SNS with a non-trivial token supply and reward rate, this can represent a meaningful amount of governance token maturity that neuron holders are entitled to but will never receive. The governance canister itself continues to operate normally; the loss is silent and unrecoverable.

### Likelihood Explanation

The trigger condition — `total_reward_shares == 0` while `considered_proposals` is non-empty — arises whenever every proposal that is ready to settle received zero votes. This is realistic for:

- A newly launched SNS whose neurons have not yet reached the minimum dissolve delay required to vote.
- An SNS in which all neurons have dissolved or been merged/split between proposal creation and reward settlement.
- Any SNS round where proposals expire unvoted (e.g., governance inactivity, network partition, or a coordinated abstention).

Because SNS deployments can be small and lightly governed, this scenario is more likely than the analogous NNS case.

### Recommendation

When `total_reward_shares == dec!(0)` and `considered_proposals` is non-empty, the function should either:

1. **Not settle the proposals** in this reward event, leaving them for the next round when neurons may vote; or
2. **Force a rollover** by treating the event as if `settled_proposals` were empty for the purpose of `e8s_equivalent_to_be_rolled_over`, so the purse is carried forward.

The simplest fix is to check `total_reward_shares == dec!(0)` before settling proposals and return early (or skip settling), preserving the purse for the next reward event.

### Proof of Concept

1. Deploy an SNS with `voting_rewards_parameters` set and a non-zero token supply.
2. Submit a proposal while all neurons are below the minimum dissolve delay (or have dissolved).
3. Advance time past the proposal's voting deadline — it becomes `ReadyToSettle` with zero ballots.
4. `distribute_rewards` is called by `run_periodic_tasks`. `rewards_purse_e8s > 0` (from supply × reward rate). `neuron_id_to_reward_shares` is empty → `total_reward_shares == 0`.
5. The `if total_reward_shares == dec!(0)` branch logs a warning and skips distribution.
6. The proposal is settled; `RewardEvent.settled_proposals` is non-empty.
7. `rewards_rolled_over()` → `false`; `e8s_equivalent_to_be_rolled_over()` → `0`.
8. The next reward round starts with a fresh purse. The maturity from step 4 is permanently gone.

### Citations

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
