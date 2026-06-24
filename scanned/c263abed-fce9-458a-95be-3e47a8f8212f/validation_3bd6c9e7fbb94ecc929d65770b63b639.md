### Title
Unbounded Instruction Consumption in SNS Governance Vote Cascade - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS Governance canister's `cast_vote_and_cascade_follow` function performs an unbounded BFS traversal over the entire follower graph in a single synchronous message execution, with no instruction-limit check or pagination. Any neuron holder with the `Vote` permission can trigger this via the public `manage_neuron` ingress endpoint, causing the SNS governance canister to exceed the IC's per-message instruction limit (40 billion instructions), trapping the message and potentially making the canister unresponsive to votes.

### Finding Description

The SNS Governance canister's `register_vote` function calls `Governance::cast_vote_and_cascade_follow` synchronously: [1](#0-0) 

`cast_vote_and_cascade_follow` performs a BFS over the entire follower graph with no instruction-limit guard: [2](#0-1) 

The outer `while !induction_votes.is_empty()` loop and the inner `for` loops over `follower_neuron_ids` have **no call to any instruction-counter check** (no `is_message_over_threshold`, no `noop_self_call_if_over_instructions`, no soft/hard limit). The function is a plain synchronous `fn`, not `async fn`, so it cannot yield to a timer or self-call.

This is in direct contrast to the NNS Governance canister's equivalent function, which was explicitly refactored to use a `ProposalVotingStateMachine` with soft/hard instruction limits and async self-calls: [3](#0-2) [4](#0-3) 

The NNS changelog explicitly documents this class of fix: [5](#0-4) 

The SNS version received no analogous fix. The SNS `cast_vote_and_cascade_follow` is a static `fn` (not `async`) and is called synchronously from `register_vote`, which is itself called from the `manage_neuron` update endpoint: [6](#0-5) 

### Impact Explanation

When a large SNS has many neurons with a star or chain following topology, a single `manage_neuron { RegisterVote }` ingress call triggers the BFS over all followers in one message. With the IC's hard per-message instruction limit of 40 billion instructions: [7](#0-6) 

...the message traps with `CanisterInstructionLimitExceeded`. The vote is not recorded (the trap rolls back state), and the canister remains functional but **no neuron whose vote would cascade through a large follower graph can ever successfully vote** on that proposal. This effectively freezes governance participation for proposals with large follower fan-out, preventing quorum from being reached and blocking SNS governance decisions.

### Likelihood Explanation

Any SNS with a moderately large neuron count and a popular "named neuron" or hub neuron that many others follow can trigger this. The attacker only needs to be a legitimate neuron holder with `Vote` permission — a standard, unprivileged role. The `manage_neuron` endpoint is publicly reachable via ingress. No special keys or governance majority are required. As SNS DAOs grow in neuron count, the probability of hitting the limit increases organically.

### Recommendation

Refactor SNS `cast_vote_and_cascade_follow` to mirror the NNS implementation:
1. Convert it to an `async fn` and introduce a `VotingStateMachine` that checkpoints BFS progress.
2. Add soft/hard instruction-limit checks inside the BFS loop (analogous to `is_message_over_threshold` / `noop_self_call_if_over_instructions` used in NNS).
3. Offload remaining work to a timer task when the soft limit is reached, as done in NNS governance.

### Proof of Concept

1. Deploy an SNS with N neurons (e.g., N = 50,000) where neurons 2..N all follow neuron 1 on a given proposal action.
2. Create a proposal of that action type; all N neurons receive ballots.
3. As neuron 1's controller, submit `manage_neuron { RegisterVote { proposal_id, vote: Yes } }`.
4. The SNS governance canister enters `cast_vote_and_cascade_follow`, iterates over all N-1 followers in a single synchronous BFS with no instruction guard.
5. The message traps with `CanisterInstructionLimitExceeded`; the vote is rolled back; neuron 1's ballot remains `Unspecified`.
6. No subsequent vote by neuron 1 can succeed either, since the follower graph has not changed.

The root cause is at: [8](#0-7) 

compared to the NNS fix at: [9](#0-8)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L3931-3942)
```rust
        Governance::cast_vote_and_cascade_follow(
            proposal_id,
            neuron_id,
            vote,
            function_id,
            &self.function_followee_index,
            &self.topic_follower_index,
            &self.proto.neurons,
            now_seconds,
            &mut proposal.ballots,
            proposal_topic.unwrap_or_default(),
        );
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

**File:** rs/nns/governance/src/voting.rs (L506-521)
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
```

**File:** rs/nns/governance/CHANGELOG.md (L667-668)
```markdown
* Unstaking maturity task will be processing up to 100 neurons in a single message, to avoid
  exceeding the instruction limit in a single execution.
```

**File:** rs/sns/governance/canister/canister.rs (L397-408)
```rust
#[update]
async fn manage_neuron(request: ManageNeuron) -> ManageNeuronResponse {
    log!(INFO, "manage_neuron");
    let governance = governance_mut();
    let result = measure_span_async(
        governance.profiling_information,
        "manage_neuron",
        governance.manage_neuron(&sns_gov_pb::ManageNeuron::from(request), &caller()),
    )
    .await;
    ManageNeuronResponse::from(result)
}
```

**File:** rs/config/src/subnet_config.rs (L36-36)
```rust
pub(crate) const MAX_INSTRUCTIONS_PER_MESSAGE: NumInstructions = NumInstructions::new(40 * B);
```
