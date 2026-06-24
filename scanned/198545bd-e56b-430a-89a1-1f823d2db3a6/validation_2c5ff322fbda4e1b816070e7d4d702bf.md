### Title
SNS Governance Unbounded Proposal Processing Loop via `allowed_when_resources_are_low()` Bypass Causes Persistent DoS - (`rs/sns/governance/src/governance.rs`)

### Summary

SNS governance enforces `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` to cap the number of open proposals, but three action types bypass this limit via `allowed_when_resources_are_low()`. The `process_proposals()` and `distribute_rewards()` functions iterate over all proposals and their ballots without any instruction-limit guard. If enough bypass proposals accumulate, these functions consistently exceed the IC instruction limit, permanently stalling governance: proposals are never settled, ballots are never cleared, and the cap is never freed, so no new regular proposals can be submitted either.

### Finding Description

**The limit and the bypass**

`MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS = 700` is the soft cap on proposals that still carry ballots. [1](#0-0) 

The cap is enforced in `make_proposal()`:

```rust
if self.proto.proposals.values()
    .filter(|data| !data.ballots.is_empty())
    .count() >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
    && !proposal.allowed_when_resources_are_low()
``` [2](#0-1) 

The bypass is granted to three action types:

```rust
pub(crate) fn allowed_when_resources_are_low(&self) -> bool {
    match self {
        Action::UpgradeSnsControlledCanister(_) => true,
        Action::UpgradeSnsToNextVersion(_) => true,
        Action::ExecuteGenericNervousSystemFunction(_) => true,
        _ => false,
    }
}
``` [3](#0-2) 

`ExecuteGenericNervousSystemFunction` is particularly broad: any of the up to `MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS = 200,000` registered custom functions qualifies. Any SNS token holder with sufficient stake can submit these proposals indefinitely past the 700-proposal cap.

**The unbounded processing loop**

`process_proposals()` collects every open proposal and calls `process_proposal()` on each one. `process_proposal()` calls `recompute_tally()`, which iterates over every ballot in the proposal. There is no instruction-limit check anywhere in this path:

```rust
pub fn process_proposals(&mut self) {
    ...
    let pids = self.proto.proposals.iter()
        .filter(|(_, info)| info.status() == Open || info.accepts_vote(...))
        .map(|(pid, _)| *pid)
        .collect::<Vec<u64>>();

    for pid in pids {
        self.process_proposal(pid);   // recompute_tally over all ballots
    }
    ...
}
``` [4](#0-3) 

**The reward-distribution loop**

`distribute_rewards()` iterates over every `ReadyToSettle` proposal and then over every ballot in each proposal to accumulate reward shares:

```rust
for proposal_id in &considered_proposals {
    if let Some(proposal) = self.get_proposal_data(*proposal_id) {
        for (voter, ballot) in &proposal.ballots {
            ...
            match NeuronId::from_str(voter) {
                Ok(neuron_id) => {
                    *neuron_id_to_reward_shares.entry(neuron_id)... += reward_shares;
                }
            }
        }
    }
}
``` [5](#0-4) 

With 700+ proposals and up to 200,000 eligible neurons per proposal, this is on the order of 140 million iterations of string parsing (`NeuronId::from_str`), `HashMap` insertions, and `Decimal` arithmetic — well above the 40 B instruction limit.

**The self-reinforcing stuck state**

`distribute_rewards()` is called from `run_periodic_tasks()`. [6](#0-5)  If it traps due to instruction limit exceeded, the timer callback fails silently. Critically, the proposals are **not** settled: `reward_event_end_timestamp_seconds` is never set and `ballots.clear()` is never reached. [7](#0-6)  The proposals remain in `ReadyToSettle` with full ballot maps, still counting toward `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS`. Every subsequent timer invocation hits the same over-limit loop and traps again. The governance canister enters a permanent state where:

1. No new regular proposals can be submitted (cap is full).
2. Bypass proposals can still be submitted, making the queue larger.
3. `distribute_rewards()` and `process_proposals()` consistently trap.
4. Voting rewards are never distributed.

### Impact Explanation

The SNS governance canister becomes non-functional for all regular proposal types. Voting rewards stop being distributed. The state is self-reinforcing: the only way out would be an emergency upgrade of the SNS governance canister itself (which is itself a bypass proposal, so it can still be submitted and voted on — but only if enough voting power exists to pass it before the reward-distribution failure causes further degradation).

### Likelihood Explanation

Triggering this requires holding enough SNS governance tokens to submit many `ExecuteGenericNervousSystemFunction` proposals (each costs `reject_cost_e8s`, typically 1 governance token). An attacker must first fill the queue with 700 regular proposals (or wait for organic accumulation during a busy period), then submit bypass proposals to push the ballot-iteration cost over the instruction limit. The conditions are specific but reachable by a motivated, well-funded attacker or spontaneously during a period of high governance activity combined with slow reward-distribution rounds.

### Recommendation

1. **Add an instruction-limit guard inside `process_proposals()`** analogous to the guard already present in NNS governance's `process_voting_state_machines()`, which uses `noop_self_call_if_over_instructions()` to break out of the loop before exhausting the budget. [8](#0-7) 

2. **Add a per-round cap on the number of proposals settled in `distribute_rewards()`** so that a single timer invocation processes at most N proposals, carrying the remainder to the next round.

3. **Apply the `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` cap to bypass proposals as well**, or introduce a separate, lower cap specifically for bypass proposals to prevent unbounded accumulation.

### Proof of Concept

1. Register a `GenericNervousSystemFunction` in the SNS.
2. Submit 700 `Motion` proposals (or any regular type) to fill the cap.
3. Once the cap is reached, submit additional `ExecuteGenericNervousSystemFunction` proposals (bypass). Each is accepted past the cap.
4. Wait for the voting period of the first 700 proposals to expire; they become `ReadyToSettle`.
5. Observe that `run_periodic_tasks()` → `distribute_rewards()` traps on every invocation due to instruction limit exceeded (iterating 700+ proposals × N neurons ballots).
6. Observe that proposals are never settled, ballots are never cleared, and no new regular proposals can be submitted.

### Citations

**File:** rs/sns/governance/src/proposal.rs (L78-79)
```rust
/// The maximum number of unsettled proposals (proposals for which ballots are still stored).
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 700;
```

**File:** rs/sns/governance/src/governance.rs (L2007-2044)
```rust
    pub fn process_proposals(&mut self) {
        if self.env.now() < self.closest_proposal_deadline_timestamp_seconds {
            // Nothing to do.
            return;
        }

        let pids = self
            .proto
            .proposals
            .iter()
            .filter(|(_, info)| {
                info.status() == ProposalDecisionStatus::Open || info.accepts_vote(self.env.now())
            })
            .map(|(pid, _)| *pid)
            .collect::<Vec<u64>>();

        for pid in pids {
            self.process_proposal(pid);
        }

        self.closest_proposal_deadline_timestamp_seconds = self
            .proto
            .proposals
            .values()
            .filter(|data| data.status() == ProposalDecisionStatus::Open)
            .map(|proposal_data| {
                proposal_data
                    .wait_for_quiet_state
                    .map(|w| w.current_deadline_timestamp_seconds)
                    .unwrap_or_else(|| {
                        proposal_data
                            .proposal_creation_timestamp_seconds
                            .saturating_add(proposal_data.initial_voting_period_seconds)
                    })
            })
            .min()
            .unwrap_or(u64::MAX);
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

**File:** rs/nns/governance/src/voting.rs (L254-266)
```rust
            if let Err(e) = noop_self_call_if_over_instructions(
                SOFT_VOTING_INSTRUCTIONS_LIMIT,
                Some(HARD_VOTING_INSTRUCTIONS_LIMIT),
            )
            .await
            {
                println!(
                    "Used too many instructions in process_voting_state_machines, \
                       exiting before finishing: {}",
                    e
                );
                break;
            }
```
