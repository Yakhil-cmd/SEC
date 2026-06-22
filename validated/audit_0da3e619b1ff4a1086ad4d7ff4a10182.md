### Title
SNS Governance `compute_ballots_for_new_proposal` Performs Unbounded Linear Neuron Scan on Every Proposal Submission, Enabling Instruction-Limit DoS - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS Governance canister's `compute_ballots_for_new_proposal` function performs a synchronous O(N) iteration over every registered neuron in heap memory to build the electoral roll each time a proposal is submitted. Unlike NNS Governance, which was updated with explicit instruction-limit guards and multi-message splitting for the equivalent operation, SNS Governance has no such protection. Any SNS token holder can stake tokens to claim neurons up to the governance-configurable `max_number_of_neurons` ceiling. If enough neurons are registered, every `make_proposal` call will trap due to instruction-limit exhaustion, permanently blocking new proposal submission and freezing SNS governance.

### Finding Description

**Root cause — unbounded linear scan in `compute_ballots_for_new_proposal`:**

`rs/sns/governance/src/governance.rs` line 5255 iterates over the entire `self.proto.neurons` `BTreeMap` (heap-resident) in a single synchronous loop:

```rust
for (k, v) in self.proto.neurons.iter() {
    if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
        continue;
    }
    let voting_power = v.voting_power(...);
    total_power += voting_power as u128;
    electoral_roll.insert(k.clone(), Ballot { ... });
}
``` [1](#0-0) 

This function is called unconditionally from `make_proposal` at line 3557:

```rust
let (_, electoral_roll) = self
    .compute_ballots_for_new_proposal()
    .map_err(...)?;
``` [2](#0-1) 

There is no instruction-counter check, no soft/hard limit guard, and no mechanism to split the work across multiple messages.

**Contrast with NNS Governance — which has been patched:**

NNS Governance explicitly defines hard and soft instruction limits for voting operations and uses multi-message splitting via `noop_self_call_if_over_instructions`:

```rust
const HARD_VOTING_INSTRUCTIONS_LIMIT: u64 = 750 * BILLION;
const SOFT_VOTING_INSTRUCTIONS_LIMIT: u64 = BILLION;
``` [3](#0-2) 

NNS Governance's `compute_ballots_for_standard_proposal` was also refactored to use a pre-computed voting-power snapshot rather than iterating over all neurons inline. The NNS CHANGELOG explicitly records fixes such as "Unstaking maturity task has a limit of 100 neurons per message, which prevents it from exceeding instruction limit" and "Avoid applying `approve_genesis_kyc` to an unbounded number of neurons." SNS Governance has received none of these mitigations. [4](#0-3) 

**Attacker-controlled entry path:**

1. Any unprivileged principal stakes SNS tokens and calls `manage_neuron` → `ClaimOrRefresh` to register neurons. The only gate is `max_number_of_neurons`, a governance parameter configurable up to `MAX_NUMBER_OF_NEURONS_CEILING`. [5](#0-4) 

2. The SNS community (or a well-funded attacker who accumulates voting power) can raise `max_number_of_neurons` via a `ManageNervousSystemParameters` proposal.

3. Once enough neurons exist, every subsequent `make_proposal` call executes the O(N) loop, consuming instructions proportional to the neuron count. When the IC per-message instruction limit (~40 billion) is reached, the call traps, the state rolls back, and no new proposal can ever be submitted.

**Instruction-cost projection:**

The NNS Governance benchmark `compute_ballots_for_new_proposal_with_stable_neurons` measures ~2,450,000 instructions for 100 neurons (~24,500 per neuron) and projects ~25 billion instructions at 500,000 neurons — approaching the 40-billion limit. [6](#0-5) [7](#0-6) 

SNS Governance's per-neuron cost is higher: it uses heap `BTreeMap<String, Neuron>` (string keys, heap allocation), computes a more complex voting-power formula with four bonus parameters, and inserts into a `BTreeMap<String, Ballot>`. At a conservative 60,000 instructions per neuron, the limit is hit at ~667,000 neurons. `MAX_NUMBER_OF_NEURONS_CEILING` for SNS is not shown in the indexed files, but the constraints test confirms it is sized to accommodate swap participants, developer neurons, and Neurons' Fund neurons — potentially in the hundreds of thousands. [8](#0-7) 

### Impact Explanation

If the neuron count reaches the instruction-exhaustion threshold, every `make_proposal` call traps. No new proposals can be submitted. The SNS governance is frozen: no upgrades, no parameter changes, no treasury transfers, no dapp management. Existing open proposals can still be voted on and executed, but once they settle, the SNS is permanently ungovernable unless neuron owners voluntarily dissolve and disburse enough neurons to reduce the count — a coordination problem that may be infeasible in a decentralized setting. This matches the Ditto analog: linear-time computation over a growing registration table causes complete protocol DoS.

### Likelihood Explanation

Medium. The attack requires either (a) a large number of independent token holders who each claim neurons, or (b) a single well-funded attacker who acquires enough tokens to fill the neuron table. The `max_number_of_neurons` ceiling must be set high enough, which requires a governance proposal to pass. However, SNS communities routinely raise this limit to accommodate growth, and the absence of any instruction-limit guard means the vulnerability is latent in every SNS deployment. The NNS Governance team has already recognized and fixed this exact class of bug in their own canister, confirming it is a realistic concern.

### Recommendation

1. **Add instruction-limit protection to `compute_ballots_for_new_proposal`** in `rs/sns/governance/src/governance.rs`. Mirror the NNS Governance approach: check `ic_cdk::api::instruction_counter()` inside the loop and either abort with an error or split into multiple messages using `noop_self_call_if_over_instructions`.
2. **Pre-compute and cache a voting-power snapshot** (as NNS Governance now does) so that `make_proposal` reads from a snapshot rather than iterating all neurons inline.
3. **Enforce a tighter `max_number_of_neurons` ceiling** calibrated against the per-neuron instruction cost to guarantee the loop always completes within the IC instruction limit.

### Proof of Concept

```
1. Deploy an SNS with max_number_of_neurons = MAX_NUMBER_OF_NEURONS_CEILING.
2. Distribute SNS tokens to N principals (N approaching the ceiling).
3. Each principal calls manage_neuron { ClaimOrRefresh } to register a neuron
   with dissolve_delay >= neuron_minimum_dissolve_delay_to_vote_seconds.
4. Once N neurons are registered, call make_proposal from any neuron.
5. Execution enters compute_ballots_for_new_proposal at line 5255,
   iterates over all N neurons, exhausts the 40B instruction limit, and traps.
6. The call is rolled back; the proposal is never created.
7. All subsequent make_proposal calls trap identically — governance is frozen.
``` [9](#0-8) [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3448-3560)
```rust
    /// Preconditions:
    /// - the proposal is successfully validated
    /// - the proposer neuron exists
    /// - the caller has the permission to make a proposal in the proposer
    ///   neuron's name (permission `SubmitProposal`)
    /// - the proposer is eligible to vote (the dissolve delay is more than
    ///   min_dissolve_delay_for_vote)
    /// - the proposer's stake is at least the reject_cost_e8s
    /// - there are not already too many proposals that still contain ballots
    pub async fn make_proposal(
        &mut self,
        proposer_id: &NeuronId,
        caller: &PrincipalId,
        proposal: &Proposal,
    ) -> Result<ProposalId, GovernanceError> {
        let now_seconds = self.env.now();

        // Validate proposal
        // TODO: return the optional extension spec
        let (rendering, action_auxiliary) = self.validate_and_render_proposal(proposal).await?;

        let nervous_system_parameters = self.nervous_system_parameters_or_panic();

        // This should not panic, because the proposal was just validated.
        let action = proposal.action.as_ref().expect("No action.");

        // These cannot be the target of a ExecuteGenericNervousSystemFunction proposal.
        let disallowed_target_canister_ids = hashset! {
            self.proto.root_canister_id_or_panic(),
            self.proto.ledger_canister_id_or_panic(),
            self.env.canister_id(),
            // TODO add ledger archives
            // TODO add swap canister here?
        };

        self.mode().allows_proposal_action_or_err(
            action,
            &disallowed_target_canister_ids,
            &self.proto.id_to_nervous_system_functions,
        )?;

        let reject_cost_e8s = nervous_system_parameters
            .reject_cost_e8s
            .expect("NervousSystemParameters must have reject_cost_e8s");

        // Before actually modifying anything, we first make sure that
        // the neuron is allowed to make this proposal and create the
        // electoral roll.
        //
        // Find the proposing neuron.
        let proposer = self.get_neuron_result(proposer_id)?;

        // === Validation
        //
        // Check that the caller is authorized to make a proposal
        proposer.check_authorized(caller, NeuronPermissionType::SubmitProposal)?;

        let min_dissolve_delay_for_vote = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .expect("NervousSystemParameters must have min_dissolve_delay_for_vote");

        let proposer_dissolve_delay = proposer.dissolve_delay_seconds(now_seconds);
        if proposer_dissolve_delay < min_dissolve_delay_for_vote {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "The proposer's dissolve delay {proposer_dissolve_delay} is less than the minimum required dissolve delay of {min_dissolve_delay_for_vote}"
                ),
            ));
        }

        // If the current stake of the proposer neuron is less than the cost
        // of having a proposal rejected, the neuron cannot make a proposal.
        if proposer.stake_e8s() < reject_cost_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Neuron doesn't have enough stake to submit proposal.",
            ));
        }

        // Check that there are not too many proposals.  What matters
        // here is the number of proposals for which ballots have not
        // yet been cleared, because ballots take the most amount of
        // space.
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

        // === Preparation
        //
        // Every neuron with a dissolve delay of at least
        // NervousSystemParameters.neuron_minimum_dissolve_delay_to_vote_seconds
        // is allowed to vote, with a voting power determined at the time of the
        // proposal creation (i.e., now).
        //
        // The electoral roll to put into the proposal.
        let (_, electoral_roll) = self
            .compute_ballots_for_new_proposal()
            .map_err(|err| GovernanceError::new_with_message(ErrorType::PreconditionFailed, err))?;

```

**File:** rs/sns/governance/src/governance.rs (L5225-5295)
```rust
    /// Computes the total potential voting power of the governance canister and ballots.
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
        let now_seconds = self.env.now();

        let nervous_system_parameters = self.nervous_system_parameters_or_panic();

        // Voting power bonus parameters.
        let max_dissolve_delay = nervous_system_parameters
            .max_dissolve_delay_seconds
            .expect("NervousSystemParameters must have max_dissolve_delay_seconds");

        let max_age_bonus = nervous_system_parameters
            .max_neuron_age_for_age_bonus
            .expect("NervousSystemParameters must have max_neuron_age_for_age_bonus");

        let max_dissolve_delay_bonus_percentage = nervous_system_parameters
            .max_dissolve_delay_bonus_percentage
            .expect("NervousSystemParameters must have max_dissolve_delay_bonus_percentage");

        let max_age_bonus_percentage = nervous_system_parameters
            .max_age_bonus_percentage
            .expect("NervousSystemParameters must have max_age_bonus_percentage");

        let min_dissolve_delay_for_vote = nervous_system_parameters
            .neuron_minimum_dissolve_delay_to_vote_seconds
            .expect("NervousSystemParameters must have min_dissolve_delay_for_vote");

        let mut electoral_roll = BTreeMap::<String, Ballot>::new();
        let mut total_power: u128 = 0;

        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }

        if total_power >= (u64::MAX as u128) {
            // The way the neurons are configured, the total voting
            // power on this proposal would overflow a u64!
            return Err("Voting power overflow.".to_string());
        }
        if electoral_roll.is_empty() {
            // Cannot make a proposal with no eligible voters.  This
            // is a precaution that shouldn't happen as we check that
            // the voter is allowed to vote.
            return Err("No eligible voters.".to_string());
        }

        Ok((total_power as u64, electoral_roll))
    }
```

**File:** rs/sns/governance/src/governance.rs (L6363-6379)
```rust
    /// Checks whether new neurons can be added or whether the maximum number of neurons,
    /// as defined in the nervous system parameters, has already been reached.
    fn check_neuron_population_can_grow(&self) -> Result<(), GovernanceError> {
        let max_number_of_neurons = self
            .nervous_system_parameters_or_panic()
            .max_number_of_neurons
            .expect("NervousSystemParameters must have max_number_of_neurons");

        if (self.proto.neurons.len() as u64) + 1 > max_number_of_neurons {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Cannot add neuron. Max number of neurons reached.",
            ));
        }

        Ok(())
    }
```

**File:** rs/nns/governance/src/voting.rs (L22-31)
```rust
/// The hard limit for the number of instructions that can be executed in a single call context.
/// This leaves room for 750 thousand neurons with complex following.
const HARD_VOTING_INSTRUCTIONS_LIMIT: u64 = 750 * BILLION;
// For production, we want this higher so that we can process more votes, but without affecting
// the overall responsiveness of the canister. 1 Billion seems like a reasonable compromise.
const SOFT_VOTING_INSTRUCTIONS_LIMIT: u64 = if cfg!(feature = "test") {
    1_000_000
} else {
    BILLION
};
```

**File:** rs/nns/governance/CHANGELOG.md (L655-675)
```markdown
        * Distribute rewards is moved to timer, and has a mechanism to distribute in batches in
          multiple messages.
        * Unstaking maturity task has a limit of 100 neurons per message, which prevents it from
          exceeding instruction limit.
        * The execution of `ApproveGenesisKyc` proposals have a limit of 1000 neurons, above which
          the proposal will fail.
        * More benchmarks were added.
* Enable timer task metrics for better observability.

## Changed

* Voting Rewards will be scheduled by a timer instead of by heartbeats.
* Unstaking maturity task will be processing up to 100 neurons in a single message, to avoid
  exceeding the instruction limit in a single execution.
* Voting Rewards will be distributed asynchronously in the background after being calculated.
    * This will allow rewards to be compatible with neurons being stored in Stable Memory.
* Ramp up the failure rate of _pb method to 0.7 again.

## Fixed

* Avoid applying `approve_genesis_kyc` to an unbounded number of neurons, but at most 1000 neurons.
```

**File:** rs/nns/governance/canbench/canbench_results.yml (L44-50)
```yaml
  compute_ballots_for_new_proposal_with_stable_neurons:
    total:
      calls: 1
      instructions: 2450000
      heap_increase: 0
      stable_memory_increase: 256
    scopes: {}
```

**File:** rs/nns/governance/src/governance/benches.rs (L467-479)
```rust
    let bench_result = bench_fn(|| {
        governance
            .compute_ballots_for_standard_proposal(123_456_789)
            .expect("Failed!");
    });

    check_projected_instructions(
        bench_result,
        num_neurons,
        MAX_NUMBER_OF_NEURONS as u64,
        25_000_000_000,
    )
}
```

**File:** rs/nervous_system/integration_tests/tests/constraints_dependencies.rs (L1-55)
```rust
use ic_nervous_system_common::MAX_NEURONS_FOR_DIRECT_PARTICIPANTS;
use ic_nns_governance::governance::MAX_NEURONS_FUND_PARTICIPANTS;
use ic_sns_governance::pb::v1::NervousSystemParameters;
use ic_sns_init::{MAX_SNS_NEURONS_PER_BASKET, distributions::MAX_DEVELOPER_DISTRIBUTION_COUNT};

// Test that the total number of SNS neurons created by an SNS swap is within the ceiling expected
// by SNS Governance (`MAX_NUMBER_OF_NEURONS_CEILING`). Concretely, the test compares this constant
// against the sum of intermediate limits set for various types of SNS neurons. These intermediate
// limits are not checked within just one canister, so testing their inter-consistency is done here.
//
// Many SNS neurons may be created after a swap succeeds. The number of such neurons is limited to
// `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`. This limit is enforced only *during* the swap. In effect,
// this limits the maximum number of swap participants to `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS` /
// #number of SNS neurons per participant (a.k.a., the SNS basket count).
//
// If a `CreateServiceNervousSystem` proposal is valid, its parameters must comply, in particular,
// with the following limits (checked at the time of proposal submission):
// - The number of SNS neurons per basket does not exceed `MAX_SNS_NEURONS_PER_BASKET`.
// - The number of SNS neurons granted to the dapp developers doe snot exceed
//   `MAX_DEVELOPER_DISTRIBUTION_COUNT`.
//
// However, the number of Neurons' Fund participants created by the swap in the worst case cannot be
// determined until the proposal is being executed (as before that, NNS neurons can opt in or out of
// the Neurons' Fund). Thus, the corresponding validation cannot be done at proposal submission time
// and is done by a different canister (NNS Governance, which currently implements the Neurons' Fund
// and is responsible for executing `CreateServiceNervousSystem` proposals).
//
// The main reason the number of SNS neurons must be limited is to avoid running out of memory in
// SNS Governance. Since SNS neurons originate from different sources (direct / Neuron's Fund swap
// participation; developer neurons; neurons created by staking SNS tokens after the swap), there
// are multiple intermediate limits used to ensure the overall `MAX_NUMBER_OF_NEURONS_CEILING`.
// This test checks that all intermediate limits are consistent, i.e., their sum does not exceed
// the ceiling expected by SNS Governance.
#[test]
fn test_max_number_of_sns_neurons_adds_up() {
    const RECOMMENDATION: &str = "If you are adjusting any of these limits, please consider the \
        risks associated with the *order* in which the affected canisters could be *upgraded*. \
        If some of these limits are being decreased, first release NNS Governance and SNS-W, \
        then publish SNS Governance. If some of these limits are being INCREASED, first publish \
        SNS Governance, then wait until all potentially affected SNSes are upgraded, and only then \
        upgrade NNS Governance and SNS-W.";
    assert!(
        NervousSystemParameters::MAX_NUMBER_OF_NEURONS_CEILING
            >= MAX_SNS_NEURONS_PER_BASKET * MAX_NEURONS_FUND_PARTICIPANTS
                + MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
                + MAX_DEVELOPER_DISTRIBUTION_COUNT as u64,
        "MAX_NUMBER_OF_NEURONS_CEILING ({}) must be >= \
         MAX_SNS_NEURONS_PER_BASKET ({MAX_SNS_NEURONS_PER_BASKET}) * \
         MAX_NEURONS_FUND_PARTICIPANTS ({MAX_NEURONS_FUND_PARTICIPANTS}) \
         + MAX_NEURONS_FOR_DIRECT_PARTICIPANTS ({MAX_NEURONS_FOR_DIRECT_PARTICIPANTS}) \
         + MAX_DEVELOPER_DISTRIBUTION_COUNT ({MAX_DEVELOPER_DISTRIBUTION_COUNT}).\n\
         {RECOMMENDATION}",
        NervousSystemParameters::MAX_NUMBER_OF_NEURONS_CEILING
    );
}
```

**File:** rs/sns/governance/src/proposal.rs (L78-82)
```rust
/// The maximum number of unsettled proposals (proposals for which ballots are still stored).
pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS: usize = 700;

/// The maximum number of GenericNervousSystemFunctions the system allows.
pub const MAX_NUMBER_OF_GENERIC_NERVOUS_SYSTEM_FUNCTIONS: usize = 200_000;
```
