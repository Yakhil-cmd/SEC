### Title
Unbounded Synchronous BFS in SNS `cast_vote_and_cascade_follow` Enables Instruction-Exhaustion DOS on Victim Neuron Voting - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister's `cast_vote_and_cascade_follow` function performs a fully synchronous, unbounded BFS traversal of the follower graph with no instruction-limit checkpoints. An unprivileged attacker who creates many neurons and has each follow a victim neuron can cause the victim's `register_vote` ingress call to exhaust the IC per-message instruction limit and trap, permanently preventing the victim from voting on any proposal where the attacker's neurons hold ballots.

### Finding Description

The SNS governance `register_vote` handler calls `cast_vote_and_cascade_follow` synchronously: [1](#0-0) 

`cast_vote_and_cascade_follow` is a plain synchronous function that runs a BFS `while !induction_votes.is_empty()` loop with no instruction-limit checks, no self-call mechanism, and no timer-based continuation: [2](#0-1) 

The follower sets consulted in each BFS tier are unbounded on the **followee side**: `max_followees_per_function` (ceiling = 15) limits how many neurons a single neuron may *follow*, but there is no limit on how many neurons may follow a given neuron: [3](#0-2) 

The `follow` function only enforces the outbound limit: [4](#0-3) 

Any neuron owner can call `follow` to register their neuron as a follower of any other neuron, with no restriction on the total number of followers a target neuron may accumulate: [5](#0-4) 

The `function_followee_index` and `topic_follower_index` are unbounded maps; the BFS reads all followers of the voted neuron in one synchronous pass: [6](#0-5) 

**Contrast with NNS governance**, which uses a `ProposalVotingStateMachine` with explicit soft/hard instruction-limit checkpoints and async self-calls to continue processing across multiple messages: [7](#0-6) [8](#0-7) 

The SNS implementation has none of these safeguards.

### Impact Explanation

When a victim neuron votes on a proposal, `register_vote` → `cast_vote_and_cascade_follow` must synchronously iterate over every follower of the victim that holds a ballot for that proposal. If the attacker has pre-populated enough follower neurons (each created before the proposal was submitted so they hold ballots), the BFS iteration exhausts the IC's per-message instruction limit (~20 billion instructions for update calls). The call traps, the vote is not recorded, and the victim cannot vote on that proposal. Because the attacker's neurons persist across proposals, every future proposal where the attacker's neurons are eligible repeats the DOS. The victim's governance participation is effectively nullified.

**Impact: High** — complete loss of voting ability for the targeted neuron on any proposal where attacker neurons hold ballots.

### Likelihood Explanation

The attack requires the attacker to stake tokens in many neurons (up to `max_number_of_neurons = 200,000` per SNS) before the targeted proposal is submitted. The minimum stake per neuron (`neuron_minimum_stake_e8s`) makes this capital-intensive but not impossible for a motivated adversary targeting a high-value SNS. The attacker does not need any privileged role; `follow` and `claim_or_refresh` are open to any principal. The attack is persistent once set up.

**Likelihood: Low** — economically costly but technically straightforward for a well-funded attacker.

### Recommendation

1. **Add instruction-limit checkpoints to SNS `cast_vote_and_cascade_follow`**, mirroring the NNS `ProposalVotingStateMachine` pattern: break out of the BFS when a soft instruction limit is reached and store intermediate state for continuation in a timer job.
2. **Enforce a per-followee follower cap** in the `follow` function: reject a `Follow` call if the target followee already has more than a configured maximum number of followers for that function/topic, analogous to how `max_followees_per_function` caps the outbound direction.
3. **Alternatively**, adopt the NNS async voting state machine architecture for SNS governance so that large follower graphs are processed incrementally across multiple messages.

### Proof of Concept

1. Attacker creates `N` neurons in the target SNS (each with minimum stake), where `N` is large enough to exhaust instructions during BFS (empirically determinable per SNS configuration).
2. Each attacker neuron calls `manage_neuron { Follow { function_id: X, followees: [victim_neuron_id] } }` — this is permitted with no follower-count check.
3. A proposal of function type `X` is submitted (by anyone). All `N` attacker neurons and the victim neuron receive ballots.
4. Victim calls `manage_neuron { RegisterVote { proposal: P, vote: Yes } }`.
5. `register_vote` → `cast_vote_and_cascade_follow` enters the BFS, finds `N` followers of the victim in `function_followee_index`, iterates over all of them synchronously with no instruction-limit escape hatch.
6. The call exhausts the instruction limit and traps. The victim's vote is not recorded. The victim cannot vote on proposal `P`.
7. The attack repeats for every subsequent proposal where the attacker neurons hold ballots, requiring no further action from the attacker. [9](#0-8) [10](#0-9) [11](#0-10)

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

**File:** rs/sns/governance/src/governance.rs (L3962-3977)
```rust
    pub fn follow(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        f: &manage_neuron::Follow,
    ) -> Result<(), GovernanceError> {
        // The implementation of this method is complicated by the
        // fact that we have to maintain a reverse index of all follow
        // relationships, i.e., the `function_followee_index`.
        let neuron = self.proto.neurons.get_mut(&id.to_string()).ok_or_else(||
            // The specified neuron is not present.
            GovernanceError::new_with_message(ErrorType::NotFound, format!("Follower neuron not found: {id}")))?;

        // Check that the caller is authorized to change followers (same authorization
        // as voting required).
        neuron.check_authorized(caller, NeuronPermissionType::Vote)?;
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

**File:** rs/sns/governance/src/types.rs (L383-386)
```rust
    /// This is an upper bound for `max_number_of_neurons`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    /// See also: `MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`.
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;
```

**File:** rs/sns/governance/src/types.rs (L408-410)
```rust
    /// This is an upper bound for `max_followees_per_function`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_FOLLOWEES_PER_FUNCTION_CEILING: u64 = 15;
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

**File:** rs/nns/governance/src/voting.rs (L514-551)
```rust
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
