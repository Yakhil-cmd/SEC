### Title
NNS/SNS Governance Proposal Queue Exhaustion via Bounded Ballot Retention - (File: rs/nns/governance/src/governance.rs, rs/sns/governance/src/governance.rs)

---

### Summary

Both NNS and SNS governance canisters enforce a hard cap on the number of proposals whose ballots have not yet been cleared. An unprivileged neuron holder can fill this bounded queue by submitting proposals at low cost. Because ballots are only cleared after a periodic reward event (approximately daily), proposals remain counted against the limit for the entire voting period plus the time until the next reward event — even after they are decided (rejected). During this window, new proposals from any other participant are blocked.

---

### Finding Description

**Bounded queue with delayed clearing:**

The NNS governance enforces `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS = 200` and the SNS governance enforces `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS = 700`. The limit check in both `make_proposal()` implementations counts proposals whose `ballots` map is non-empty:

```rust
// NNS: rs/nns/governance/src/governance.rs
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
        ErrorType::ResourceExhausted, ...
    ));
}
``` [1](#0-0) 

```rust
// SNS: rs/sns/governance/src/governance.rs
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
        ErrorType::ResourceExhausted, ...
    ));
}
``` [2](#0-1) 

The constants are: [3](#0-2) [4](#0-3) 

**Ballots are not cleared on decision — only on reward settlement:**

A proposal's ballots remain populated until the periodic reward event settles the proposal and clears them. This is confirmed by the existing test, which shows that even after all proposals are rejected and `run_periodic_tasks()` is called, the limit is still enforced because no reward event has yet cleared the ballots: [5](#0-4) 

**Wait-for-quiet extends the window further:**

The NNS wait-for-quiet algorithm can extend a proposal's voting deadline by up to `2 × WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS = 4 days` when a vote flips the majority near the deadline. This is explicitly acknowledged as a DoS vector in the codebase: [6](#0-5) 

The SNS equivalent uses a configurable `wait_for_quiet_deadline_increase_seconds` (default 1 day, max extension 2 days).

**Attack sequence (analogous to Taiko ring-buffer exhaustion):**

1. Attacker creates a neuron with the minimum required dissolve delay and enough tokens to

### Citations

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

**File:** rs/sns/governance/src/proposal.rs (L78-79)
```rust
/// The maximum number of unsettled proposals (proposals for which ballots are still stored).
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 700;
```

**File:** rs/nns/governance/tests/governance.rs (L8089-8117)
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
```

**File:** rs/nns/governance/src/lib.rs (L112-121)
```rust
//! measure of “voting noise”. If the threshold is too low, an
//! attacker can delay the NNS from deciding on proposals by voting
//! just as the “noise level” is about to fall beneath the threshold,
//! and it cannot be made too high, or else an attacker might try to
//! DoS the NNS so that it decides on proposals using only a small
//! proportion of the voting power that wanted to participate (since
//! it equates their not being able to vote, with their not wanting to
//! vote). Using Wait For Quiet, the NNS can decide on proposals
//! without need for a quorum of voting power to participate, and it
//! can also always decide upon proposals in a timely manner.
```
