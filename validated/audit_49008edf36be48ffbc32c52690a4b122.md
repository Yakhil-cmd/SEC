### Title
SNS Governance Proposal Submission DoS via Ballot Queue Exhaustion — (`rs/sns/governance/src/governance.rs`, `rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS governance `make_proposal` function blocks new proposals once the count of proposals with non-empty ballots reaches `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` (700). Critically, ballots are **not** cleared upon proposal rejection — they are only cleared when a reward event settles the proposal. An unprivileged user who controls a neuron with sufficient dissolve delay can fill the queue with proposals that get rejected, blocking all non-whitelisted governance actions for up to one full reward round. On SNS instances where `reject_cost_e8s = 0`, this attack is economically free.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, `make_proposal` enforces a hard cap:

```rust
if self.proto.proposals.values()
    .filter(|data| !data.ballots.is_empty())
    .count()
    >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
    && !proposal.allowed_when_resources_are_low()
{
    return Err(GovernanceError::new_with_message(
        ErrorType::ResourceExhausted,
        "Reached maximum number of proposals that have not yet \
        been taken into account for voting rewards. \
        Please try again later.",
    ));
}
``` [1](#0-0) 

The constant is defined as:

```rust
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 700;
``` [2](#0-1) 

The filter `!data.ballots.is_empty()` counts **all** proposals whose ballots have not yet been cleared — including proposals that have already been decided (rejected or adopted) but not yet settled in a reward event. Ballots are only cleared during `distribute_rewards`, which runs once per `round_duration_seconds` (configurable, default ~1 week):

```rust
fn distribute_rewards(&mut self, supply: Tokens) {
    ...
    let considered_proposals: Vec<ProposalId> =
        self.ready_to_be_settled_proposal_ids().collect();
    ...
}
``` [3](#0-2) 

The `can_be_purged` function confirms that garbage collection also cannot remove proposals until they are rewarded:

```rust
pub(crate) fn can_be_purged(&self, now_seconds: u64) -> bool {
    if !self.status().is_final() { return false; }
    if !self.reward_status(now_seconds).is_final() { return false; }
    ...
}
``` [4](#0-3) 

`reward_status` is only `Settled` (final) after `has_been_rewarded()` returns true, which requires `reward_event_end_timestamp_seconds.is_some() || reward_event_round > 0`: [5](#0-4) 

The `allowed_when_resources_are_low()` escape hatch for SNS covers only three action types:

```rust
pub(crate) fn allowed_when_resources_are_low(&self) -> bool {
    match self {
        Action::UpgradeSnsControlledCanister(_) => true,
        Action::UpgradeSnsToNextVersion(_) => true,
        Action::ExecuteGenericNervousSystemFunction(_) => true,
        _ => false,
    }
}
``` [6](#0-5) 

`ManageNervousSystemParameters` is **not** in this whitelist, meaning the SNS cannot pass a governance proposal to raise `max_number_of_proposals_with_ballots` or increase `reject_cost_e8s` while the queue is saturated.

The NNS governance has the same structural issue with `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS = 200`: [7](#0-6) [8](#0-7) 

The NNS GC also cannot clear proposals until they are settled: [9](#0-8) 

The NNS test `test_max_number_of_proposals_with_ballots` explicitly demonstrates that even after all 200 proposals are rejected and `run_periodic_tasks` is called, new proposals remain blocked until a reward event fires: [10](#0-9) 

---

### Impact Explanation

An attacker who saturates the SNS ballot queue blocks all `Motion`, `ManageNervousSystemParameters`, `TransferSnsTreasuryFunds`, `MintSnsTokens`, and `RegisterDappCanisters` proposals for up to one full reward round (configurable, typically days to a week). Because `ManageNervousSystemParameters` is not whitelisted, the SNS community cannot pass a governance fix while the queue is full. The only relief is waiting for the next automatic reward event to clear ballots. A sustained attacker can re-fill the queue immediately after each reward event, creating a persistent governance DoS.

---

### Likelihood Explanation

For SNS instances with `reject_cost_e8s = 0` (a valid and common configuration), the attack costs only the initial stake to create a neuron with sufficient dissolve delay — effectively free. For SNS instances with non-zero `reject_cost_e8s`, the cost is `700 × reject_cost_e8s` in SNS tokens, which may be low depending on token price. The attacker-controlled entry path is a standard unprivileged `manage_neuron` ingress call with `MakeProposal` command, requiring no special privileges. No admin key, governance majority, or threshold corruption is needed.

---

### Recommendation

1. **Clear ballots on rejection**: When a proposal is rejected (decided but not adopted), clear its ballots immediately in `process_proposal` rather than waiting for the reward event. Reward eligibility tracking can be maintained separately without retaining the full ballot map.
2. **Exclude decided proposals from the cap**: Change the `make_proposal` filter from `!data.ballots.is_empty()` to `data.status() == ProposalDecisionStatus::Open` so that only genuinely open proposals count toward the limit.
3. **Whitelist `ManageNervousSystemParameters`**: Add it to `allowed_when_resources_are_low()` so the SNS can always pass parameter changes to recover from a saturated queue.
4. **Enforce a minimum `reject_cost_e8s`**: Require a non-zero rejection fee to raise the economic cost of spam proposals.

---

### Proof of Concept

1. Deploy or target an SNS with `reject_cost_e8s = 0` and `initial_voting_period_seconds` set to a short value.
2. Create a neuron with any stake and dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`.
3. Submit 700 `Motion` proposals via `manage_neuron { command: MakeProposal(...) }`.
4. Wait for the voting period to expire; all proposals are rejected automatically by `process_proposals` (called from the periodic timer).
5. Attempt to submit a new `Motion` or `ManageNervousSystemParameters` proposal — it is rejected with `ResourceExhausted: "Reached maximum number of proposals that have not yet been taken into account for voting rewards."`.
6. The block persists until `distribute_rewards` fires (one `round_duration_seconds` later), at which point the attacker can immediately repeat from step 3.

### Citations

**File:** rs/sns/governance/src/governance.rs (L3532-3547)
```rust
        if self
            .proto
            .proposals
            .values()
            .filter(|data| !data.ballots.is_empty())
            .count()
            >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
            && !proposal.allowed_when_resources_are_low()
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached maximum number of proposals that have not yet \
                been taken into account for voting rewards. \
                Please try again later.",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L5763-5823)
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
```

**File:** rs/sns/governance/src/proposal.rs (L79-79)
```rust
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 700;
```

**File:** rs/sns/governance/src/proposal.rs (L2073-2075)
```rust
    pub fn has_been_rewarded(&self) -> bool {
        self.reward_event_end_timestamp_seconds.is_some() || self.reward_event_round > 0
    }
```

**File:** rs/sns/governance/src/proposal.rs (L2437-2444)
```rust
    pub(crate) fn can_be_purged(&self, now_seconds: u64) -> bool {
        // Retain proposals that have not gone through the full lifecycle.
        if !self.status().is_final() {
            return false;
        }
        if !self.reward_status(now_seconds).is_final() {
            return false;
        }
```

**File:** rs/sns/governance/src/types.rs (L1751-1762)
```rust
    pub(crate) fn allowed_when_resources_are_low(&self) -> bool {
        match self {
            // Due to possible need of an emergency upgrade of the dapp
            Action::UpgradeSnsControlledCanister(_) => true,
            // Due to possible need of an emergency upgrade of the SNS
            Action::UpgradeSnsToNextVersion(_) => true,
            // Due to possible need of emergency functions defined as
            // GenericNervousSystemFunctions
            Action::ExecuteGenericNervousSystemFunction(_) => true,
            _ => false,
        }
    }
```

**File:** rs/nns/governance/src/governance.rs (L250-252)
```rust
/// The max number of unsettled proposals -- that is proposals for which ballots
/// are still stored.
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 200;
```

**File:** rs/nns/governance/src/governance.rs (L5254-5269)
```rust
            if self
                .heap_data
                .proposals
                .values()
                .filter(|info| !info.ballots.is_empty() && !info.is_manage_neuron())
                .count()
                >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
                && !action.allowed_when_resources_are_low()
            {
                return Err(GovernanceError::new_with_message(
                    ErrorType::ResourceExhausted,
                    "Reached maximum number of proposals that have not yet \
                    been taken into account for voting rewards. \
                    Please try again later.",
                ));
            }
```

**File:** rs/nns/governance/src/garbage_collection.rs (L93-106)
```rust
    pub fn can_be_purged(&self, now_seconds: u64, voting_period_seconds: u64) -> bool {
        if !self.status().is_final() {
            return false;
        }

        if !self
            .reward_status(now_seconds, voting_period_seconds)
            .is_final()
        {
            return false;
        }

        true
    }
```

**File:** rs/nns/governance/tests/governance.rs (L8089-8139)
```rust
    fake_driver.advance_time_by(10);
    gov.run_periodic_tasks().now_or_never().unwrap();
    run_pending_timers().await;

    // Now all proposals should have been rejected.
    for i in 1_u64..MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS as u64 + 2 {
        assert_eq!(
            gov.get_proposal_data(ProposalId { id: i })
                .unwrap()
                .status(),
            Rejected
        );
    }

    // But we still can't submit new proposals.
    // Let's try one more. It should be rejected.
    assert_matches!(gov.make_proposal(
        &NeuronId { id: 1 },
        // Must match neuron 1's serialized_id.
        &principal(1),
        &Proposal {
            title: Some("A Reasonable Title".to_string()),
            summary: "this one should not make it though...".to_string(),
            action: Some(proposal::Action::Motion(Motion {
                motion_text: "so many proposals!".to_string(),
            })),
            ..Default::default()
        },
    ).await, Err(GovernanceError{error_type, error_message: _}) if error_type==ResourceExhausted as i32);

    // Let's make a reward event happen
    fake_driver.advance_time_by(REWARD_DISTRIBUTION_PERIOD_SECONDS);
    gov.run_periodic_tasks().now_or_never().unwrap();
    run_pending_timers().await;

    // Now it should be allowed to submit a new one
    gov.make_proposal(
        &NeuronId { id: 1 },
        // Must match neuron 1's serialized_id.
        &principal(1),
        &Proposal {
            title: Some("A Reasonable Title".to_string()),
            summary: "Now it should work!".to_string(),
            action: Some(proposal::Action::Motion(Motion {
                motion_text: "did it?".to_string(),
            })),
            ..Default::default()
        },
    )
    .await
    .unwrap();
```
