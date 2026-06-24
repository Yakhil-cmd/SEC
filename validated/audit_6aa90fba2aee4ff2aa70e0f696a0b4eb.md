Audit Report

## Title
SNS Governance `cast_vote_and_cascade_follow` Unbounded Synchronous BFS Exhausts Instruction Limit - (File: rs/sns/governance/src/governance.rs)

## Summary
The SNS governance canister's `cast_vote_and_cascade_follow` function performs a fully synchronous, unbounded BFS over the follower graph with no instruction-limit guard. Because there is no cap on how many neurons may follow a given neuron, an unprivileged attacker can create arbitrarily many neurons all following a single target, causing every subsequent vote or proposal by that target to trap and permanently silence it. NNS governance already carries the fix (async state machine with soft/hard instruction-limit checks); SNS governance does not.

## Finding Description

**Synchronous BFS with no instruction-limit guard**

`cast_vote_and_cascade_follow` is declared as a plain synchronous `fn` at [1](#0-0)  The BFS `while` loop at [2](#0-1)  iterates over every follower of every newly-voted neuron with no call to any instruction-limit check and no mechanism to yield and resume. A repository-wide search for `is_over_instructions_limit`, `noop_self_call_if_over_instructions`, `SOFT_VOTING_INSTRUCTIONS_LIMIT`, and `HARD_VOTING_INSTRUCTIONS_LIMIT` returns zero matches in all SNS governance source files, confirming the absence of any guard.

**Both public entry points call it synchronously**

`make_proposal` invokes it synchronously at [3](#0-2)  and `register_vote` invokes it synchronously at [4](#0-3)  Neither entry point is `async`, so there is no opportunity to yield between BFS iterations.

**No cap on incoming follow edges**

`max_followees_per_function` (validated in `validate_max_followees_per_function`) limits only outgoing edges — how many neurons a single neuron may follow. [5](#0-4)  There is no corresponding `max_followers_per_neuron` parameter and no enforcement at `follow`/`set_following` time on incoming edges. A repository-wide search for `max_followers`, `follower.*limit`, and `limit.*follower` in SNS governance returns zero matches.

**NNS governance already has the fix**

NNS governance's `cast_vote_and_cascade_follow` is `async` and wraps the BFS in a loop that calls `noop_self_call_if_over_instructions` with explicit `SOFT_VOTING_INSTRUCTIONS_LIMIT` and `HARD_VOTING_INSTRUCTIONS_LIMIT` constants, breaking out to a timer job when the soft limit is approached. [6](#0-5)  The inner BFS processing function `continue_processing` additionally checks `is_over_instructions_limit()` after every neuron processed. [7](#0-6)  SNS governance has neither mechanism.

## Impact Explanation

When the BFS over all follower neurons exceeds the ~5 billion instruction limit for a single update call, the call traps and the vote is never recorded. Because the follower graph is persistent state, every subsequent attempt by the target neuron to vote on any proposal will also trap. This permanently silences any influential neuron in SNS governance, breaking liveness of the voting process for all future proposals. This matches the allowed High impact: **Application/platform-level DoS with concrete user or protocol harm** — specifically, permanent disruption of SNS governance voting for targeted neurons.

## Likelihood Explanation

Any token holder can create neurons in an SNS and configure following without any privileged role. The attacker must stake enough tokens to create `N` neurons (each with stake ≥ `neuron_minimum_stake_e8s` and dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`) and set each to follow the target. For SNS instances with low token prices or a high `max_number_of_neurons` ceiling, the economic cost is low. The attack is persistent: once the follower graph is set up, every future proposal silences the target neuron. The asymmetry with NNS governance (which already carries the fix) confirms the developers recognized this risk class.

## Recommendation

1. **Adopt the NNS async state-machine pattern in SNS governance**: Make `cast_vote_and_cascade_follow` (or its callers `register_vote` and `make_proposal`) async, introduce `SOFT_VOTING_INSTRUCTIONS_LIMIT` / `HARD_VOTING_INSTRUCTIONS_LIMIT` constants, call `noop_self_call_if_over_instructions` in the outer BFS loop, and continue processing in a timer job (`process_voting_state_machines`) when the soft limit is reached.

2. **Add `is_over_instructions_limit()` checks inside the inner BFS loop**: Mirror the NNS `continue_processing` pattern so the function returns early (preserving partial state) rather than trapping.

3. **Cap incoming follow edges**: Introduce a `max_followers_per_neuron` parameter enforced at `follow`/`set_following` time to bound BFS fan-out at write time.

## Proof of Concept

1. Deploy an SNS with a reasonably large `max_number_of_neurons` (e.g., 10,000).
2. As an unprivileged user, create `N` neurons each with stake ≥ `neuron_minimum_stake_e8s` and dissolve delay ≥ `neuron_minimum_dissolve_delay_to_vote_seconds`.
3. For each attacker neuron, call `manage_neuron { Follow { function_id: <target_function_id>, followees: [<target_neuron_id>] } }`.
4. Wait for a proposal to be created (all `N` attacker neurons and the target receive ballots).
5. Have the target neuron call `manage_neuron { RegisterVote { proposal_id, vote: Yes } }`.
6. `register_vote` calls `cast_vote_and_cascade_follow` synchronously; the BFS iterates over all `N` followers with no instruction-limit check, exhausting the per-message instruction budget.
7. The update call traps. The target neuron's vote is never recorded. Every subsequent vote attempt by the target neuron on any proposal repeats this outcome — the neuron is permanently silenced.

A deterministic integration test using PocketIC can reproduce this by creating `N` follower neurons, triggering a vote, and asserting that the `register_vote` call returns a trap/error and that the target neuron's ballot remains `Unspecified`.

### Citations

**File:** rs/sns/governance/src/governance.rs (L3658-3669)
```rust
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
