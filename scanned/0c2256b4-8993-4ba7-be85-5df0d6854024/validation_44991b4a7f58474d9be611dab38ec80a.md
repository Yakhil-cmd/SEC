### Title
Unbounded Synchronous BFS in SNS `cast_vote_and_cascade_follow` Enables Griefing via Follower Flooding - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance canister's `cast_vote_and_cascade_follow` function performs a fully synchronous, unbounded BFS over all follower neurons when a neuron votes. Because any unprivileged user can create neurons and set following relationships, an attacker can flood a target neuron's follower set (up to `MAX_NUMBER_OF_NEURONS_CEILING = 200,000`) before a proposal is created. When the target neuron subsequently calls `register_vote`, the synchronous BFS exhausts the IC's per-message instruction limit, causing the update call to trap and the vote to be permanently unrecorded for that message execution.

### Finding Description

**Root cause — synchronous, unbounded BFS without instruction-limit guard:**

In `rs/sns/governance/src/governance.rs`, `cast_vote_and_cascade_follow` is a plain synchronous function that runs a BFS over the entire follower graph in a single message execution:

```rust
fn cast_vote_and_cascade_follow(
    ...
    neurons: &BTreeMap<String, Neuron>,
    ballots: &mut BTreeMap<String, Ballot>,
    ...
) {
    ...
    while !induction_votes.is_empty() {
        let mut follower_neuron_ids = BTreeSet::new();
        for (current_neuron_id, _) in &induction_votes {
            // Iterates over ALL followers of current_neuron_id
            if let Some(new_follower_neuron_ids) = topic_followers
                .and_then(|tf| tf.get(current_neuron_id))
            {
                for id in new_follower_neuron_ids { follower_neuron_ids.insert(id.clone()); }
            }
            ...
        }
        for follower_neuron_id in follower_neuron_ids {
            let follower_vote = follower_neuron.vote_from_ballots_following(...);
            ...
        }
    }
}
```

There is **no** `is_over_instructions_limit` check, no `noop_self_call_if_over_instructions` await point, and no state-machine checkpointing. The function is called synchronously from `register_vote`:

```rust
fn register_vote(...) -> Result<(), GovernanceError> {
    ...
    Governance::cast_vote_and_cascade_follow(
        proposal_id, neuron_id, vote, function_id,
        &self.function_followee_index, &self.topic_follower_index,
        &self.proto.neurons, now_seconds,
        &mut proposal.ballots, proposal_topic.unwrap_or_default(),
    );
    ...
}
```

**Contrast with NNS governance**, which uses an async state machine with explicit soft/hard instruction-limit guards:

```rust
pub async fn cast_vote_and_cascade_follow(&mut self, ...) {
    while !is_voting_finished {
        with_voting_state_machines_mut(|vsm| {
            vsm.with_machine(proposal_id, topic, |machine| {
                self.process_machine_until_soft_limit(machine, over_soft_message_limit);
                is_voting_finished = machine.is_voting_finished();
            });
        });
        if let Err(e) = noop_self_call_if_over_instructions(
            SOFT_VOTING_INSTRUCTIONS_LIMIT,
            Some(HARD_VOTING_INSTRUCTIONS_LIMIT),
        ).await { break; }
    }
}
```

The SNS version has no equivalent protection.

**Attacker-controlled entry path:**

1. Attacker stakes the minimum `neuron_minimum_stake_e8s` tokens and creates up to `max_number_of_neurons` neurons (ceiling `MAX_NUMBER_OF_NEURONS_CEILING = 200,000`).
2. Each attacker neuron calls `follow` (or `set_following`) to follow the victim neuron on a specific function/topic. There is no limit on how many neurons may follow a given neuron — only `max_followees_per_function` (ceiling 15) limits how many a single neuron can *follow*, not how many can *follow it*.
3. A proposal is created; ballots are allocated for all existing neurons including all attacker neurons.
4. The victim neuron calls `register_vote`. The synchronous BFS in `cast_vote_and_cascade_follow` must iterate over all 200,000 follower neurons, performing `BTreeMap` lookups in `neurons` (O(log N) each) and `vote_from_ballots_following` (O(max_followees_per_function) ballot lookups each). At 200,000 followers × ~32 operations each, the instruction budget is exhausted and the update call traps.

### Impact Explanation

The victim neuron's `register_vote` update call traps due to instruction exhaustion. The vote is not recorded. The victim cannot vote on the proposal during the voting period. This is a griefing attack: the attacker gains no direct profit but permanently denies the victim's voting right on any proposal created while the follower flood is in place. In an SNS with a small number of legitimate neurons, a single large-stake neuron being silenced can materially affect governance outcomes.

### Likelihood Explanation

The attack requires the attacker to stake tokens in many neurons. However:
- `neuron_minimum_stake_e8s` is SNS-configurable and can be set very low.
- `max_number_of_neurons` is also SNS-configurable (up to 200,000).
- The following relationship is set by the follower, not the followee — the victim has no ability to prevent or remove followers.
- The attack is persistent: once neurons are created and following is set, the flood remains until the attacker dissolves their neurons (which takes time due to dissolve delay).

### Recommendation

Apply the same instruction-limit-aware state machine pattern used in NNS governance to the SNS `cast_vote_and_cascade_follow`:

1. Make `register_vote` async and introduce a `ProposalVotingStateMachine` equivalent for SNS.
2. Add `is_over_instructions_limit` checks inside the BFS loop, checkpointing progress to stable state.
3. Resume processing in a timer job (analogous to `process_voting_state_machines` in NNS).
4. Alternatively, enforce a per-neuron cap on the number of neurons that may follow it (a "max followers" limit), analogous to `MAX_DELEGATES` in the original report, to bound the BFS fan-out.

### Proof of Concept

**Setup (pseudocode):**
```
// 1. Attacker creates N neurons (N = max_number_of_neurons, up to 200,000)
for i in 0..N:
    stake(neuron_minimum_stake_e8s)
    claim_neuron(i)
    follow(attacker_neuron[i], target_neuron_id, function_id=<any>)

// 2. Someone creates a proposal (ballots allocated for all neurons including attacker's)
proposal_id = make_proposal(...)

// 3. Victim tries to vote
register_vote(target_neuron_id, proposal_id, Vote::Yes)
// -> cast_vote_and_cascade_follow iterates over N followers synchronously
// -> instruction limit exceeded -> message traps -> vote not recorded
```

**Key code references:**

- Synchronous BFS with no instruction guard: [1](#0-0) 
- `register_vote` calling BFS synchronously: [2](#0-1) 
- NNS instruction-limit guard (absent in SNS): [3](#0-2) 
- `MAX_NUMBER_OF_NEURONS_CEILING = 200,000`: [4](#0-3) 
- No limit on followers per neuron (only followees per neuron is capped): [5](#0-4) 
- `follow` is callable by any neuron controller: [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3687-3837)
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
        let fallback_pseudo_function_id = u64::from(&Action::Unspecified(Empty {}));
        assert!(function_id != fallback_pseudo_function_id);

        // This identifies which other neurons might get "triggered" to vote by
        // filling in the current neuron's ballot.
        //
        // By default, followers on the specific function_id are reconsidered,
        // as well as followers have have general "catch-all" following. As an
        // optimization, catch-all followers are not considered when the
        // proposal is Critical.
        //
        // E.g. if Alice follows Bob on "catch-all", and Bob votes on a
        // TransferSnsTreasuryFunds proposal, then Alice will not be considered
        // a follower of Bob, because the proposal is Critical.
        let neuron_id_to_follower_neuron_ids = {
            let mut members = vec![];
            let mut push_member = |function_id| {
                if let Some(member) = function_followee_index.get(&function_id) {
                    members.push(member);
                }
            };

            push_member(function_id);

            match topic.proposal_criticality() {
                ProposalCriticality::Normal => push_member(fallback_pseudo_function_id),
                ProposalCriticality::Critical => (), // Do not use catch-all/fallback following.
            }

            UnionMultiMap::new(members)
        };

        let topic_followers = topic_follower_index.get(&topic);

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

**File:** rs/sns/governance/src/governance.rs (L3962-3996)
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

**File:** rs/sns/governance/src/types.rs (L386-386)
```rust
    pub const MAX_NUMBER_OF_NEURONS_CEILING: u64 = 200_000;
```

**File:** rs/sns/governance/src/types.rs (L408-410)
```rust
    /// This is an upper bound for `max_followees_per_function`. Exceeding it may cause
    /// degradation in the governance canister or the subnet hosting the SNS.
    pub const MAX_FOLLOWEES_PER_FUNCTION_CEILING: u64 = 15;
```
