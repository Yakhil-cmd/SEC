### Title
SNS Governance Voting Rewards Permanently Lost When Proposals Settle With Zero Voter Participation - (`rs/sns/governance/src/governance.rs`, `rs/sns/governance/src/types.rs`)

---

### Summary

In the SNS governance canister, when one or more proposals reach `ReadyToSettle` state during a reward round but no neurons cast eligible votes (`total_reward_shares == 0`), the entire rewards purse for that round is permanently discarded rather than rolled over to the next round. This is because the rollover predicate only checks whether `settled_proposals` is empty, not whether rewards were actually distributed. The same structural issue exists in NNS governance.

---

### Finding Description

SNS governance distributes voting rewards periodically via `distribute_rewards` in `rs/sns/governance/src/governance.rs`. The function:

1. Calculates a `rewards_purse_e8s` from the token supply, reward rate, and any rolled-over amount from the previous event.
2. Tallies voting power exercised across all `ReadyToSettle` proposals into `neuron_id_to_reward_shares`.
3. If `total_reward_shares == dec!(0)`, it logs an error and skips all maturity increments — `distributed_e8s_equivalent` stays 0.
4. Regardless of whether any rewards were distributed, it marks every proposal in `considered_proposals` as settled (sets `reward_event_end_timestamp_seconds`, clears ballots).
5. Writes a new `RewardEvent` with `settled_proposals = considered_proposals` (non-empty), `distributed_e8s_equivalent = 0`, and `total_available_e8s_equivalent = Some(rewards_purse_e8s)`. [1](#0-0) 

In the **next** reward round, `distribute_rewards` calls `e8s_equivalent_to_be_rolled_over()` on the previous event: [2](#0-1) 

`rewards_rolled_over()` returns `true` only when `settled_proposals.is_empty()`. Because the previous event has non-empty `settled_proposals`, `rewards_rolled_over()` returns `false`, and `e8s_equivalent_to_be_rolled_over()` returns **0**. The entire `rewards_purse_e8s` from the previous round — including any previously rolled-over amounts — is silently discarded.

The identical structural flaw exists in NNS governance: [3](#0-2) [4](#0-3) 

The genesis pseudo-event is initialized with `end_timestamp_seconds: Some(now)`, so reward rounds begin immediately at SNS launch: [5](#0-4) 

---

### Impact Explanation

When proposals are settled in a reward round but no neurons cast eligible votes, the entire rewards purse for that round — including any amounts rolled over from prior empty rounds — is permanently lost. These tokens are never minted as maturity for any neuron. Token holders who staked and expected inflationary rewards receive nothing for that period, and the shortfall cannot be recovered in subsequent rounds.

For a new SNS with a small neuron set (e.g., immediately after the decentralization swap, before neurons configure following), this can silently destroy multiple rounds of accumulated rewards. The `total_available_e8s_equivalent` field in the emitted `RewardEvent` records the full purse, making the loss observable on-chain but not recoverable.

---

### Likelihood Explanation

**SNS (medium):** A newly launched SNS starts reward rounds at genesis. During the swap period and immediately after, developer neurons may be the only participants. If a proposal is submitted and reaches `ReadyToSettle` before swap participants configure their neurons or following, `total_reward_shares` will be 0 and the round's rewards are lost. An unprivileged actor holding a developer neuron can deliberately submit proposals during this window to trigger the condition. No privileged access is required beyond holding a neuron.

**NNS (low):** The NNS has a large, active neuron set with well-established following chains, making `total_voting_rights < 0.001` extremely unlikely in practice. The code path exists but is rarely reachable.

---

### Recommendation

The rollover predicate should reflect whether rewards were actually distributed, not merely whether proposals were settled. Change `rewards_rolled_over()` to return `true` when `distributed_e8s_equivalent == 0` (regardless of `settled_proposals`):

```rust
pub(crate) fn rewards_rolled_over(&self) -> bool {
    self.distributed_e8s_equivalent == 0
}
```

This ensures that any round in which no maturity was actually minted — whether because there were no proposals, or because there were proposals but no eligible votes — carries its full purse forward to the next round. Apply the same fix to the NNS `RewardEvent` implementation in `rs/nns/governance/src/reward/calculation.rs`.

---

### Proof of Concept

**Scenario (SNS):**

1. SNS launches at time `T`. Genesis pseudo-event sets `end_timestamp_seconds = T`.
2. A developer neuron submits a proposal at `T + 1`. The proposal's voting period is shorter than `round_duration_seconds`.
3. No other neurons vote (swap not yet complete; no following configured). The proposal reaches `ReadyToSettle` at `T + voting_period`.
4. At `T + round_duration_seconds`, `should_distribute_rewards()` returns `true`. [6](#0-5) 

5. `distribute_rewards` is called. `rewards_purse_e8s = supply * initial_reward_rate * round_duration`. `considered_proposals = [proposal_1]`. All ballots have `vote = Unspecified` → `total_reward_shares = 0`.
6. The `if total_reward_shares == dec!(0)` branch is taken; no maturity is minted.
7. `proposal_1` is settled: `reward_event_end_timestamp_seconds` set, ballots cleared.
8. New `RewardEvent` written: `settled_proposals = [proposal_1]`, `distributed_e8s_equivalent = 0`, `total_available_e8s_equivalent = Some(rewards_purse_e8s)`. [7](#0-6) 

9. At `T + 2 * round_duration_seconds`, the next round begins. `e8s_equivalent_to_be_rolled_over()` on the previous event: `rewards_rolled_over()` → `settled_proposals.is_empty()` → `false` → returns `0`.
10. The new round's purse starts from 0 rollover. The entire first-round purse is gone.

### Citations

**File:** rs/sns/governance/src/governance.rs (L726-742)
```rust
        if proto.latest_reward_event.is_none() {
            // Introduce a dummy reward event to mark the origin of the SNS instance era.
            // This is required to be able to compute accurately the rewards for the
            // very first reward distribution.
            proto.latest_reward_event = Some(RewardEvent {
                actual_timestamp_seconds: now,
                round: 0,
                settled_proposals: vec![],
                distributed_e8s_equivalent: 0,
                end_timestamp_seconds: Some(now),
                rounds_since_last_distribution: Some(0),
                // This value should be considered equivalent to None (allowing
                // the use of unwrap_or_default), but for consistency, we
                // explicitly initialize to 0.
                total_available_e8s_equivalent: Some(0),
            })
        }
```

**File:** rs/sns/governance/src/governance.rs (L5725-5753)
```rust
    fn should_distribute_rewards(&self) -> bool {
        let now = self.env.now();

        let voting_rewards_parameters = match &self
            .nervous_system_parameters_or_panic()
            .voting_rewards_parameters
        {
            None => return false,
            Some(ok) => ok,
        };
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

**File:** rs/sns/governance/src/governance.rs (L6009-6082)
```rust
        // This field is deprecated. People should really use end_timestamp_seconds
        // instead. This value can still be used if round duration is not changed.
        let new_reward_event_round = self.latest_reward_event().round + new_rounds_count;
        // Settle proposals.
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

**File:** rs/sns/governance/src/types.rs (L2054-2067)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent.unwrap_or_default()
        } else {
            0
        }
    }

    // Not copied from NNS: fn rounds_since_last_distribution_to_be_rolled_over

    /// Whether this is a "rollover event", where no rewards were distributed.
    pub(crate) fn rewards_rolled_over(&self) -> bool {
        self.settled_proposals.is_empty()
    }
```

**File:** rs/nns/governance/src/reward/calculation.rs (L120-147)
```rust
    pub(crate) fn e8s_equivalent_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.total_available_e8s_equivalent
        } else {
            0
        }
    }

    /// Calculates the rounds_since_last_distribution in this event that should
    /// be "rolled over" into the next `RewardEvent`.
    ///
    /// Behavior:
    /// - If rewards were distributed for this event, then no rounds should be
    ///   rolled over, so this function returns 0.
    /// - Otherwise, this function returns
    ///   `rounds_since_last_distribution`.
    pub(crate) fn rounds_since_last_distribution_to_be_rolled_over(&self) -> u64 {
        if self.rewards_rolled_over() {
            self.rounds_since_last_distribution.unwrap_or(0)
        } else {
            0
        }
    }

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
