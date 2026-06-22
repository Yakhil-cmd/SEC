### Title
Unbounded BFS Iteration in `cast_vote_and_cascade_follow` Exhausts Instruction Limit - (File: `rs/sns/governance/src/governance.rs`)

### Summary
The SNS governance canister's `cast_vote_and_cascade_follow` function performs a synchronous, unbounded breadth-first search (BFS) over the neuron follower graph with no instruction-limit guard. An unprivileged attacker who creates many neurons all following a single target neuron can cause any `register_vote` or `make_proposal` call by that target neuron to trap due to instruction exhaustion, permanently preventing it from voting on proposals.

### Finding Description
`cast_vote_and_cascade_follow` in SNS governance is a synchronous function that iterates over the entire follower graph in a `while !induction_votes.is_empty()` loop: [1](#0-0) 

Each BFS tier processes all followers of the current tier's neurons, collecting the next tier, with no check against the IC instruction limit: [2](#0-1) 

This function is called synchronously both when a proposal is submitted and when a neuron registers a vote: [3](#0-2) 

By contrast, NNS governance uses a dedicated voting state machine (`cast_vote_and_cascade_follow` in `rs/nns/governance/src/voting.rs`) that detects when the instruction limit is approached and defers remaining work to a timer task across multiple messages. SNS governance has no equivalent mechanism. [4](#0-3) 

The SNS governance does enforce `max_followees_per_function` to limit how many neurons a single neuron can follow: [5](#0-4) 

However, this does not bound the number of neurons that can follow a single target neuron. Any number of neurons (up to `max_number_of_neurons`) can all follow the same target, making the BFS fan-out proportional to the total neuron count. [6](#0-5) 

### Impact Explanation
When the BFS loop exceeds the per-message instruction limit, the entire `register_vote` (or `make_proposal`) update call traps and its state changes are rolled back. The target neuron's vote is never recorded. If the target neuron is a high-weight voter or a followee of many other neurons, this prevents the proposal from reaching quorum, causing governance liveness failure for the affected SNS. The attacker does not need to hold any privileged role — only the ability to create neurons and set following relationships, which are standard unprivileged operations.

### Likelihood Explanation
The SNS `max_number_of_neurons` is configurable per-SNS and can be set to values in the thousands. An attacker with sufficient SNS tokens can create many neurons and configure them all to follow a single target neuron. The cost is proportional to the SNS neuron creation fee. Once the follower count is large enough to exhaust instructions in a single BFS pass, the DoS is persistent: every subsequent vote attempt by the target neuron will trap.

### Recommendation
Introduce an instruction-limit check inside the BFS loop in `cast_vote_and_cascade_follow` in `rs/sns/governance/src/governance.rs`, analogous to the mechanism already present in NNS governance. When the remaining instruction budget falls below a threshold, the function should stop processing the current BFS tier, persist the remaining `induction_votes` to stable state, and schedule a timer task to continue processing in a subsequent message. Alternatively, enforce a hard cap on the total number of followers per neuron (not just the number of followees per neuron) to bound the BFS fan-out.

### Proof of Concept
1. Deploy an SNS with `max_number_of_neurons = N` (e.g., 10,000).
2. As an attacker, create `N - 1` neurons and configure each to follow a single target neuron (the victim) on all function IDs.
3. Wait for the victim neuron to attempt to vote on a proposal by calling `register_vote`.
4. The SNS governance canister executes `cast_vote_and_cascade_follow` synchronously. The BFS loop must process up to `N - 1` follower neurons in a single message.
5. The message exhausts the IC instruction limit and traps. The vote is not recorded.
6. Every subsequent `register_vote` call by the victim neuron traps identically, permanently preventing it from voting.

The root cause is at: [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3655-3669)
```rust
        let function_id = u64::from(action);

        // Cast a 'yes'-vote for the proposer, including following.
        Governance::cast_vote_and_cascade_follow(
            &proposal_id,
            proposer_id,
            Vote::Yes,
            function_id,
            &self.function_followee_index,
            &self.topic_follower_index,
            &self.proto.neurons,
            now_seconds,
            &mut proposal_data.ballots,
            proposal_topic,
        );
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

**File:** rs/sns/governance/src/governance.rs (L3742-3836)
```rust
        // Each iteration of this loop processes one tier in the BFS.
        //
        // This has to terminate, because if we keep going around in a cycle, that
        // means the same neuron keeps getting swayed, but once a neuron is swayed,
        // it does not matter how its "other" followees vote (i.e. those that have
        // not (directly or indirectly) voted yet). That is, once a neuron is swayed,
        // its vote is "locked in". IOW, swaying is "monotonic".
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

**File:** rs/sns/governance/src/governance.rs (L3987-3996)
```rust
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

**File:** rs/nns/governance/src/voting.rs (L1153-1165)
```rust
    #[test]
    fn test_cast_vote_and_cascade_follow_always_finishes_processing_ballots() {
        let _a = temporarily_set_over_soft_message_limit(true);
        let topic = Topic::NetworkEconomics;
        let mut governance = Governance::new(
            Default::default(),
            Arc::new(MockEnvironment::new(Default::default(), 0)),
            Arc::new(StubIcpLedger {}),
            Arc::new(StubCMC {}),
            Box::new(MockRandomness::new()),
        );

        let mut proposal = ProposalData {
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1703-1711)
```rust
    pub default_followees: ::core::option::Option<DefaultFollowees>,
    /// The maximum number of allowed neurons. When this maximum is reached, no new
    /// neurons will be created until some are removed.
    ///
    /// This number must be larger than zero and at most as large as the defined
    /// ceiling MAX_NUMBER_OF_NEURONS_CEILING.
    #[prost(uint64, optional, tag = "7")]
    pub max_number_of_neurons: ::core::option::Option<u64>,
    /// The minimum dissolve delay a neuron must have to be eligible to vote.
```
