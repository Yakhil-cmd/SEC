### Title
Unbounded Iteration Over All Neurons During SNS Proposal Creation Causes Instruction Limit Exhaustion - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance canister's `compute_ballots_for_new_proposal` function unconditionally iterates over every neuron in the system to build the electoral roll each time any neuron holder submits a proposal. With `MAX_NUMBER_OF_NEURONS_CEILING` set to 200,000, this unbounded synchronous loop can exhaust the per-message instruction limit, permanently blocking new proposal submission and halting SNS governance. The NNS governance canister has explicit benchmarks and instruction-limit guards for the equivalent path; the SNS canister has neither.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `compute_ballots_for_new_proposal` performs a full linear scan of `self.proto.neurons`: [1](#0-0) 

Every call to `make_proposal` by any neuron holder triggers this path. There is no instruction-limit check, no batching, and no DTS continuation mechanism inside this function. The loop runs to completion or the message is killed by the replica with `CanisterInstructionLimitExceeded`.

The SNS parameter ceiling that bounds the neuron population is: [2](#0-1) 

Up to 200,000 neurons are permitted. Each iteration computes a `voting_power` value and inserts a `Ballot` into a `BTreeMap`, making the per-neuron cost non-trivial.

A second unbounded loop exists in `cast_vote_and_cascade_follow`, which runs a BFS over the follower graph with no instruction-limit guard: [3](#0-2) 

By contrast, the NNS governance canister's equivalent cascade function explicitly checks `is_over_instructions_limit()` at each step and suspends work into a persistent state machine: [4](#0-3) 

The NNS `compute_ballots_for_standard_proposal` path also has a dedicated benchmark that projects instruction usage against `MAX_NUMBER_OF_NEURONS` (500,000) and asserts it stays within a 25B-instruction budget: [5](#0-4) 

No equivalent benchmark or guard exists for the SNS path.

### Impact Explanation

If an SNS accumulates a large neuron population (reachable via swap participants, developer neurons, and post-swap stakers — all of which are explicitly tracked against `MAX_NUMBER_OF_NEURONS_CEILING`): [6](#0-5) 

…then every call to `make_proposal` will attempt to iterate all neurons in a single message. Once the neuron count is high enough to exhaust the ~40B-instruction per-message limit (application subnet), every `make_proposal` call will trap with `CanisterInstructionLimitExceeded`. Since proposals are the only mechanism for SNS governance actions (upgrades, treasury transfers, parameter changes), this constitutes a **governance halt**: the SNS becomes permanently unable to adopt new proposals, including the upgrade proposal that would fix the bug.

### Likelihood Explanation

The `MAX_NUMBER_OF_NEURONS_CEILING` of 200,000 is reachable for a popular SNS. The NNS governance benchmarks show that iterating 500,000 neurons costs ~25B instructions; scaling linearly, 200,000 neurons costs ~10B instructions per `make_proposal` call — within the application-subnet limit today, but with no safety margin and no protection against the ceiling being raised via a `ManageNervousSystemParameters` proposal. Any neuron holder (an unprivileged ingress sender) can trigger the path. No special role or key is required.

### Recommendation

1. **Mirror the NNS approach**: replace the synchronous full-scan in `compute_ballots_for_new_proposal` with a snapshot-based mechanism (analogous to `compute_voting_power_snapshot_for_standard_proposal` in the NNS) that can be pre-computed periodically and reused at proposal time.
2. **Add instruction-limit guards** to `cast_vote_and_cascade_follow` matching the NNS `is_over_instructions_limit` pattern, with a persistent state machine to resume work across messages.
3. **Add a benchmark** for `compute_ballots_for_new_proposal` at `MAX_NUMBER_OF_NEURONS_CEILING` neurons, analogous to the NNS bench, and gate CI on it staying within the instruction budget.

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons` set to a large value (e.g., 100,000).
2. Populate the SNS with neurons up to that limit via swap participation and direct staking.
3. Call `manage_neuron` → `MakeProposal` from any neuron holder.
4. Observe the call trap with `CanisterInstructionLimitExceeded` (error code 522).
5. Confirm that no further proposals can be submitted, and that the SNS cannot self-upgrade to fix the issue.

The entry path is a standard ingress `update` call to the SNS governance canister's `manage_neuron` endpoint — no privileged access required. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3749-3836)
```rust
        while !induction_votes.is_empty() {
            // This will be populated with the followers of neurons in the
            // current BFS tier, who might be swayed to indirectly vote, thus
            // forming the next tier in the BFS.
            let mut follower_neuron_ids = BTreeSet::new();

            // Process the current tier in the BFS.
            for (current_neuron_id, current_new_vote) in &induction_votes {
                let current_ballot = match ballots.get_mut(current_neuron_id) {
                    Some(b) => b,
                    None => {
                        // neuron_id has no (blank) ballot, which means they
                        // were not eligible when the proposal was first
                        // created. This is fairly unusual, but does not
                        // indicate a bug (therefore, no log).
                        continue;
                    }
                };

                // Only fill in "blank" ballots. I.e. those with vote ==
                // Unspecified. This check could just as well be done before
                // current_neuron_id is added to induction_votes.
                if current_ballot.vote != (Vote::Unspecified as i32) {
                    continue;
                }

                // Fill in current_ballot.
                assert_ne!(*current_new_vote, Vote::Unspecified);
                current_ballot.vote = *current_new_vote as i32;
                current_ballot.cast_timestamp_seconds = now_seconds;

                // Take note of the followers of current_neuron_id, and add them
                // to the next "tier" in the BFS.

                if let Some(new_follower_neuron_ids) = topic_followers
                    .and_then(|topic_followers| topic_followers.get(current_neuron_id))
                {
                    for follower_neuron_id in new_follower_neuron_ids {
                        follower_neuron_ids.insert(follower_neuron_id.clone());
                    }
                }

                if let Some(new_follower_neuron_ids) =
                    neuron_id_to_follower_neuron_ids.get(current_neuron_id)
                {
                    for follower_neuron_id in new_follower_neuron_ids {
                        follower_neuron_ids.insert(follower_neuron_id.clone());
                    }
                }
            }

            // Prepare for the next iteration of the (outer most) loop by
            // constructing the next BFS tier (from follower_neuron_ids).
            induction_votes.clear();
            for follower_neuron_id in follower_neuron_ids {
                let Some(follower_neuron) = neurons.get(&follower_neuron_id.to_string()) else {
                    // This is a highly suspicious, because currently, we do not
                    // delete neurons, which means that we have an invalid NeuronId
                    // floating around in the system, which indicates that we have a
                    // bug. For now, we deal with that by logging, and pretending like
                    // we did not see follower_neuron_id.
                    log!(
                        ERROR,
                        "Missing neuron {} while trying to record (and cascade) \
                            a vote on proposal {:#?}.",
                        follower_neuron_id,
                        proposal_id,
                    );
                    continue;
                };

                let follower_vote = follower_neuron.vote_from_ballots_following(
                    function_id,
                    topic,
                    ballots,
                    proposal_id,
                );

                if follower_vote != Vote::Unspecified {
                    // follower_neuron would be swayed by its followees!
                    //
                    // This is the other (earlier) point at which we could
                    // consider whether a neuron is already locked in, and that
                    // no recursion is needed.
                    induction_votes.insert(follower_neuron_id.to_string(), follower_vote);
                }
            }
        }
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

**File:** rs/sns/governance/src/types.rs (L383-386)
```rust
    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;
```

**File:** rs/nns/governance/src/voting.rs (L506-551)
```rust
    fn continue_processing(
        &mut self,
        neuron_store: &mut NeuronStore,
        ballots: &mut HashMap<u64, Ballot>,
        is_over_instructions_limit: fn() -> bool,
    ) {
        let voting_finished = self.is_voting_finished();

        if !voting_finished {
            while let Some(neuron_id) = self.neurons_to_check_followers.pop_first() {
                self.add_followers_to_check(neuron_store, neuron_id, self.topic);

                // Before we check the next one, see if we're over the limit.
                if is_over_instructions_limit() {
                    return;
                }
            }

            // Memory optimization, will not cause tests to fail if removed
            retain_neurons_with_castable_ballots(&mut self.followers_to_check, ballots);

            while let Some(follower) = self.followers_to_check.pop_first() {
                let vote = match neuron_store
                    .neuron_would_follow_ballots(follower, self.topic, ballots)
                {
                    Ok(vote) => vote,
                    Err(e) => {
                        // This is a bad inconsistency, but there is
                        // nothing that can be done about it at this
                        // place.  We somehow have followers recorded that don't exist.
                        eprintln!(
                            "error in cast_vote_and_cascade_follow when gathering induction votes: {:?}",
                            e
                        );
                        Vote::Unspecified
                    }
                };
                // Casting vote immediately might affect other follower votes, which makes
                // voting resolution take fewer iterations.
                // Vote::Unspecified is ignored by cast_vote.
                self.cast_vote(ballots, follower, vote);

                if is_over_instructions_limit() {
                    return;
                }
            }
```

**File:** rs/nns/governance/src/governance/benches.rs (L441-479)
```rust
fn compute_ballots_for_new_proposal_with_stable_neurons() -> BenchResult {
    let now_seconds = 1732817584;
    let num_neurons = 100;

    let mut governance = Governance::new(
        Default::default(),
        Arc::new(MockEnvironment::new(vec![], now_seconds)),
        Arc::new(StubIcpLedger {}),
        Arc::new(StubCMC {}),
        Box::new(MockRandomness::new()),
    );

    for id in 1..=num_neurons {
        governance
            .add_neuron(
                id,
                make_neuron(
                    id,
                    PrincipalId::new_user_test_id(id),
                    1_000_000_000,
                    hashmap! {}, // get the default followees
                ),
            )
            .unwrap();
    }

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

**File:** rs/nervous_system/integration_tests/tests/constraints_dependencies.rs (L1-54)
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
```
