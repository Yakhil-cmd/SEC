### Title
SNS Governance `cast_vote_and_cascade_follow` - Unbounded Follower BFS Traversal May Exhaust Instruction Limit - (File: rs/sns/governance/src/governance.rs)

---

### Summary

The SNS governance canister's `cast_vote_and_cascade_follow` function performs a synchronous, unbounded BFS over the follower graph with no instruction-limit check. There is no cap on how many neurons may follow a given neuron (only on how many a neuron may *follow*). An unprivileged user can create many neurons all following a single target neuron, causing the BFS to exhaust the per-message instruction limit when that target neuron votes or makes a proposal, permanently preventing the call from succeeding.

---

### Finding Description

**Unbounded follower sets in the reverse index**

`max_followees_per_function` limits outgoing follow edges per neuron, but there is no corresponding limit on incoming edges (i.e., how many neurons may follow a given neuron). The reverse index `function_followee_index` and `topic_follower_index` grow without bound as more neurons choose to follow a popular neuron. [1](#0-0) 

**Synchronous BFS with no instruction-limit guard**

`cast_vote_and_cascade_follow` in SNS governance is a plain synchronous function. It iterates over every follower of every newly-voted neuron in a BFS loop with no soft/hard instruction-limit check and no ability to yield and resume: [2](#0-1) 

This function is called synchronously from both `make_proposal` (when the proposer auto-votes yes) and `register_vote`: [3](#0-2) 

**Contrast with NNS governance — which already has the fix**

NNS governance explicitly handles this with an async state machine, soft/hard instruction limits, and `noop_self_call_if_over_instructions` to continue processing in a timer job if the limit is approached: [4](#0-3) 

The NNS state machine also checks `is_over_instructions_limit` inside the inner follower-processing loop: [5](#0-4) 

SNS governance has no equivalent mechanism.

**`max_followees_per_function` does not bound the BFS size**

The SNS parameter `max_followees_per_function` (ceiling `MAX_FOLLOWEES_PER_FUNCTION_CEILING`) only limits how many neurons a single neuron may follow. It does not limit how many neurons may follow a single neuron. The BFS work is proportional to the total number of neurons with ballots that transitively follow the voting neuron — which can be as large as `max_number_of_neurons`. [6](#0-5) 

---

### Impact Explanation

When a popular followee neuron calls `register_vote` (or `make_proposal`), the synchronous BFS over all its followers exhausts the ~5 billion instruction limit for that update call. The call traps and the vote is never recorded. Because the follower graph is persistent state, every subsequent attempt by that neuron to vote on any proposal will also fail. This permanently silences influential neurons in SNS governance, breaking liveness of the voting process.

---

### Likelihood Explanation

Any token holder can create neurons in an SNS and set their following to any target neuron — no privileged role is required. The attacker must stake enough tokens to create a large number of neurons and set their dissolve delays above the minimum voting threshold so they receive ballots. Once set up, the attack is persistent across all future proposals. The economic cost scales with the SNS token price and `neuron_minimum_stake_e8s`, but for SNS instances with low token prices or high `max_number_of_neurons` ceilings, the attack is practical. The asymmetry with NNS governance (which already has the fix) confirms the developers recognized this risk class.

---

### Recommendation

1. **Add instruction-limit protection to SNS `cast_vote_and_cascade_follow`**: Adopt the same async state-machine pattern used in NNS governance — check `is_over_instructions_limit` inside the BFS loop and continue processing in a timer job (`process_voting_state_machines`) if the soft limit is reached.

2. **Cap the number of followers per neuron**: Introduce a `max_followers_per_neuron` parameter (analogous to `max_followees_per_function`) enforced when a neuron calls `follow` or `set_following`, so the BFS fan-out is bounded at write time rather than at vote time.

---

### Proof of Concept

1. Deploy or interact with an SNS that has a reasonably large `max_number_of_neurons` (e.g., 10,000+).
2. As an unprivileged user, create `N` neurons (up to `max_number_of_neurons`), each with stake ≥ `neuron_minimum_stake_e8s` and dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`.
3. For each attacker neuron, call `manage_neuron { Follow { function_id: <target_function>, followees: [<target_neuron_id>] } }` so all `N` neurons follow the target neuron.
4. Wait for a proposal to be created (all `N` attacker neurons plus the target neuron receive ballots).
5. Have the target neuron call `manage_neuron { RegisterVote { proposal_id, vote: Yes } }`.
6. `cast_vote_and_cascade_follow` is invoked synchronously; the BFS iterates over all `N` followers, exhausting the instruction limit.
7. The update call traps. The target neuron's vote is never recorded. Repeat for every subsequent proposal — the target neuron is permanently silenced. [7](#0-6) [8](#0-7)

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

**File:** rs/sns/governance/src/governance.rs (L3735-3836)
```rust
        // Traverse the follow graph using breadth first search (BFS).

        // Each "tier" in the BFS is listed here. Of course, the first tier just
        // contains the original "triggering" ballot.
        let mut induction_votes = BTreeMap::new();
        induction_votes.insert(voting_neuron_id.to_string(), vote_of_neuron);

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

**File:** rs/sns/governance/src/governance.rs (L3979-3995)
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
```

**File:** rs/nns/governance/src/voting.rs (L148-176)
```rust
        let mut is_voting_finished = false;

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

**File:** rs/sns/governance/src/types.rs (L774-788)
```rust
    /// Validates that the nervous system parameter max_followees_per_function is well-formed.
    fn validate_max_followees_per_function(&self) -> Result<u64, String> {
        let max_followees_per_function = self.max_followees_per_function.ok_or_else(|| {
            "NervousSystemParameters.max_followees_per_function must be set".to_string()
        })?;

        if max_followees_per_function > Self::MAX_FOLLOWEES_PER_FUNCTION_CEILING {
            Err(format!(
                "NervousSystemParameters.max_followees_per_function ({}) cannot be greater than {}",
                max_followees_per_function,
                Self::MAX_FOLLOWEES_PER_FUNCTION_CEILING
            ))
        } else {
            Ok(max_followees_per_function)
        }
```
