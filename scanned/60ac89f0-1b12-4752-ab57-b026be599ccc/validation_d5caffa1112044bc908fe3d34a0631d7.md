### Title
SNS Governance `cast_vote_and_cascade_follow` Unbounded Synchronous BFS Can Exhaust Instruction Limit, DOSing Neuron Votes - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `cast_vote_and_cascade_follow` function performs a synchronous, unbounded BFS traversal of the follower graph with no instruction-limit guard. An unprivileged attacker who creates many neurons all following a target neuron can cause any `register_vote` call from that target neuron to exceed the IC's per-message instruction limit, permanently blocking the target neuron from voting on any proposal.

---

### Finding Description

The NNS governance canister has a well-designed, instruction-aware voting cascade. Its `cast_vote_and_cascade_follow` is `async`, uses a persistent `VotingStateMachine`, and calls `noop_self_call_if_over_instructions` with both a soft limit (`SOFT_VOTING_INSTRUCTIONS_LIMIT = 1B`) and a hard limit (`HARD_VOTING_INSTRUCTIONS_LIMIT = 750B`) to safely spread work across multiple messages. [1](#0-0) [2](#0-1) 

The SNS governance canister has **no equivalent protection**. Its `cast_vote_and_cascade_follow` is a plain synchronous function that runs a `while !induction_votes.is_empty()` BFS loop to completion in a single call, with no instruction counter check at any point in the loop body: [3](#0-2) [4](#0-3) 

The SNS `NervousSystemParameters` allows up to `MAX_NUMBER_OF_NEURONS_CEILING = 200,000` neurons and `MAX_FOLLOWEES_PER_FUNCTION_CEILING = 15` followees per function per neuron: [5](#0-4) 

An attacker creates a large number of neurons (up to the SNS's `max_number_of_neurons` limit) and sets each to follow the victim neuron on the relevant function ID. When the victim neuron calls `register_vote`, the SNS governance calls `cast_vote_and_cascade_follow`, which must BFS-traverse all follower neurons synchronously. With enough followers, this traversal exhausts the IC's 40-billion-instruction per-message limit, causing the update call to trap with `CanisterInstructionLimitExceeded`. The victim neuron's vote is never recorded.

The `follow` endpoint enforces `max_followees_per_function` on the *follower* side (how many neurons one neuron can follow), but places **no limit on how many neurons can follow a single neuron** (the fan-in). This is the exploitable asymmetry. [6](#0-5) 

---

### Impact Explanation

A victim neuron that has accumulated many followers (legitimately or via attacker-created neurons) cannot vote on any proposal. Every `register_vote` call traps before the ballot is recorded. Since SNS proposals have fixed voting periods, a blocked neuron permanently loses its ability to vote on active proposals. If the victim neuron is a large stakeholder or a named neuron that many others follow, the cascade failure also prevents all downstream follower neurons from having their ballots filled in automatically, suppressing a large fraction of the SNS's voting power.

---

### Likelihood Explanation

The attack requires creating many neurons, each requiring a minimum stake (`neuron_minimum_stake_e8s`). For SNS instances with low minimum stakes (e.g., 1 token), the cost is low. The attacker does not need any special permissions: `follow` and `claim_or_refresh_neuron` are both publicly accessible update methods. The attacker only needs to hold enough SNS tokens to stake the follower neurons, which can be acquired on the open market. The attack is permanent for the duration of any active proposal.

---

### Recommendation

Apply the same instruction-limit-aware pattern used in NNS governance to the SNS `cast_vote_and_cascade_follow`:

1. Convert `cast_vote_and_cascade_follow` in `rs/sns/governance/src/governance.rs` to an `async` function backed by a persistent voting state machine (analogous to `ProposalVotingStateMachine` in NNS).
2. Add a soft instruction limit check inside the BFS loop body, yielding via a self-call when the limit is approached.
3. Add a hard instruction limit to prevent the canister from being permanently stuck.
4. Alternatively, enforce a per-neuron cap on the number of *incoming* followers (fan-in limit), mirroring the existing fan-out limit (`max_followees_per_function`).

---

### Proof of Concept

1. Deploy an SNS with `max_number_of_neurons = 200_000` and a low `neuron_minimum_stake_e8s`.
2. Create N attacker neurons (e.g., N = 50,000), each calling `follow` to follow victim neuron V on function ID F.
3. Create a proposal of type F.
4. Have victim neuron V call `register_vote` (Yes or No) on the proposal.
5. Observe that the update call traps with `CanisterInstructionLimitExceeded` because `cast_vote_and_cascade_follow` must synchronously BFS-traverse all N follower neurons.
6. Confirm that V's ballot remains `Unspecified` in the proposal's ballot map — V's vote was never recorded.

The relevant synchronous BFS with no guard: [7](#0-6)

### Citations

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

**File:** rs/nns/governance/src/voting.rs (L150-176)
```rust
        while !is_voting_finished {
            // Now we process until we are done or we are over a limit and need to
            // make a self-call.
            with_voting_state_machines_mut(|voting_state_machines| {
                voting_state_machines.with_machine(proposal_id, topic, |machine| {
                    self.process_machine_until_soft_limit(machine, over_soft_message_limit);
                    is_voting_finished = machine.is_voting_finished();
                });
            });

            // This returns an error if we hit the hard limit, which should basically never happen
            // in production, but we need a way out of this loop in the worst case to prevent
            // the canister from being unable to upgrade.
            if let Err(e) = noop_self_call_if_over_instructions(
                SOFT_VOTING_INSTRUCTIONS_LIMIT,
                Some(HARD_VOTING_INSTRUCTIONS_LIMIT),
            )
            .await
            {
                println!(
                    "Error in cast_vote_and_cascade_follow, \
                        voting will be processed in timers: {}",
                    e
                );
                break;
            }
        }
```

**File:** rs/sns/governance/src/governance.rs (L3687-3700)
```rust
    fn cast_vote_and_cascade_follow(
        proposal_id: &ProposalId, // As of Nov, 2023 (a2095be), this is only used for logging.
        voting_neuron_id: &NeuronId,
        vote_of_neuron: Vote,
        function_id: u64,
        function_followee_index: &legacy::FollowerIndex,
        topic_follower_index: &FollowerIndex,
        neurons: &BTreeMap<String, Neuron>,
        // As of Dec, 2023 (52eec5c), the next parameter is only used to populate Ballots. In
        // particular, this has no impact on how the implications of following are deduced.
        now_seconds: u64,
        ballots: &mut BTreeMap<String, Ballot>, // This is ultimately what gets changed.
        topic: Topic,
    ) {
```

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

**File:** rs/sns/governance/src/governance.rs (L3979-3996)
```rust
        let max_followees_per_function = self
            .proto
            .parameters
            .as_ref()
            .expect("NervousSystemParameters not present")
            .max_followees_per_function
            .expect("NervousSystemParameters must have max_followees_per_function");

        // Check that the list of followees is not too
        // long. Allowing neurons to follow too many neurons
        // allows a memory exhaustion attack on the neurons
        // canister.
        if f.followees.len() > max_followees_per_function as usize {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "Too many followees.",
            ));
        }
```

**File:** rs/sns/governance/src/types.rs (L383-415)
```rust
    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;

    /// This is an upper bound for `max_number_of_proposals_with_ballots`. Exceeding
    /// it may cause degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS_CEILING: u64 = 700;

    /// This is an upper bound for `initial_voting_period_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const INITIAL_VOTING_PERIOD_SECONDS_CEILING: u64 = 30 * ONE_DAY_SECONDS;

    /// This is a lower bound for `initial_voting_period_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const INITIAL_VOTING_PERIOD_SECONDS_FLOOR: u64 = ONE_DAY_SECONDS;

    /// This is an upper bound for `wait_for_quiet_deadline_increase_seconds`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_CEILING: u64 = 30 * ONE_DAY_SECONDS;

    /// This is a lower bound for `wait_for_quiet_deadline_increase_seconds`. We're setting it to
    /// 1 instead of 0 because values of 0 are not currently well-tested.
    pub const WAIT_FOR_QUIET_DEADLINE_INCREASE_SECONDS_FLOOR: u64 = 1;

    /// This is an upper bound for `max_followees_per_function`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_FOLLOWEES_PER_FUNCTION_CEILING: u64 = 15;

    /// This is an upper bound for `max_number_of_principals_per_neuron`. Exceeding
    /// it may cause may cause degradation in the governance canister or the subnet
    /// hosting the SNS.
    pub const MAX_NUMBER_OF_PRINCIPALS_PER_NEURON_CEILING: u64 = 15;
```
