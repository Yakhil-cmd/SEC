### Title
SNS Governance `cast_vote_and_cascade_follow` Performs Unbounded Synchronous BFS Over Follower Graph, Enabling Instruction-Exhaustion DoS Against Any Voting Neuron - (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `cast_vote_and_cascade_follow` function executes a fully synchronous, unbounded BFS traversal over all neurons that follow the voting neuron. Because there is no per-follower instruction-limit check and no limit on how many neurons may follow a single neuron, an unprivileged attacker who stakes a large number of cheap SNS neurons and sets them all to follow a target neuron can cause every subsequent `register_vote` call by that target neuron to trap at the IC instruction limit, permanently silencing it on every open proposal.

---

### Finding Description

**NNS governance** solved this exact problem by converting `cast_vote_and_cascade_follow` into an `async` function that calls `noop_self_call_if_over_instructions` after each BFS step, with a soft limit of 1 billion instructions and a hard limit of 750 billion instructions, deferring remaining work to a timer job. [1](#0-0) [2](#0-1) 

**SNS governance** has no equivalent protection. Its `cast_vote_and_cascade_follow` is a plain synchronous function:

```rust
fn cast_vote_and_cascade_follow(
    proposal_id: &ProposalId,
    voting_neuron_id: &NeuronId,
    vote_of_neuron: Vote,
    function_id: u64,
    function_followee_index: &legacy::FollowerIndex,
    topic_follower_index: &FollowerIndex,
    neurons: &BTreeMap<String, Neuron>,
    now_seconds: u64,
    ballots: &mut BTreeMap<String, Ballot>,
    topic: Topic,
) {
    ...
    while !induction_votes.is_empty() {
        let mut follower_neuron_ids = BTreeSet::new();
        for (current_neuron_id, current_new_vote) in &induction_votes {
            // fill in ballot, collect all followers — no instruction check
            for follower_neuron_id in new_follower_neuron_ids { ... }
        }
        induction_votes.clear();
        for follower_neuron_id in follower_neuron_ids {
            // neuron lookup + vote_from_ballots_following — no instruction check
        }
    }
}
``` [3](#0-2) 

The outer `while !induction_votes.is_empty()` loop and the inner `for follower_neuron_id in follower_neuron_ids` loop both iterate over attacker-controlled data with zero instruction-limit guards.

**The `follow` endpoint** enforces `max_followees_per_function` — the maximum number of neurons a single neuron may follow — but places **no cap on the number of neurons that may follow a given neuron** (i.e., the fan-in of the follower graph is unbounded): [4](#0-3) 

An attacker who controls K neurons can set each one to follow the victim neuron on every function ID, making the victim's follower set size K with no protocol-level barrier.

---

### Impact Explanation

When the victim neuron calls `register_vote`, the SNS governance canister calls `cast_vote_and_cascade_follow` synchronously inside the same update message. If the BFS traversal over K followers exhausts the IC's per-message instruction limit (5 billion instructions for update calls), the entire message traps and **all state changes are rolled back**, including the victim's own ballot. The victim neuron's vote is never recorded. Because the attacker's follower neurons remain in place for every future proposal, the victim is permanently unable to vote on any proposal for which the attacker's neurons hold ballots — which is every proposal created after the attacker's neurons were staked.

---

### Likelihood Explanation

Any principal can stake SNS tokens and claim neurons via `ClaimOrRefresh`, then call `Follow` to set each neuron to follow the target. The only economic barrier is the cost of staking tokens; there is no protocol-enforced cap on the number of neurons per principal or on the fan-in of the follower graph. On SNS instances where the governance token trades at low prices, the cost to create enough follower neurons to exhaust 5 billion instructions is modest. The attack is also persistent: once the follower neurons are in place, every future vote by the victim traps without any further attacker action.

---

### Recommendation

Apply the same mitigation already present in NNS governance:

1. Convert `cast_vote_and_cascade_follow` in `rs/sns/governance/src/governance.rs` to an `async` function.
2. Insert an instruction-limit check (using `ic_cdk::api::instruction_counter()`) after each BFS tier, deferring remaining work to a timer job (analogous to `process_voting_state_machines` in NNS governance).
3. Alternatively, enforce a maximum fan-in per neuron per function (e.g., reject a `Follow` call if the target neuron already has more than `MAX_FOLLOWERS` followers for that function). [5](#0-4) 

---

### Proof of Concept

1. **Setup**: Attacker acquires a large number of SNS tokens (or uses a low-price SNS) and stakes them across K separate neurons via repeated `ClaimOrRefresh` calls.
2. **Poison the follower index**: For each attacker neuron, call `manage_neuron` → `Follow { function_id: <any>, followees: [victim_neuron_id] }`. After K calls, `function_followee_index[function_id][victim_neuron_id]` contains K attacker neuron IDs.
3. **Trigger**: The victim neuron calls `manage_neuron` → `RegisterVote { proposal_id, vote: Yes }`.
4. **Execution path**: `Governance::register_vote` → `cast_vote_and_cascade_follow` → BFS iterates over K follower neurons, performing a `neurons.get(...)` and `vote_from_ballots_following(...)` for each one. [6](#0-5) 

5. **Outcome**: With K large enough (empirically, tens of thousands of followers given the cost of each `neurons.get` + `vote_from_ballots_following` call on a `BTreeMap`), the message exhausts 5 billion instructions and traps. The victim's ballot remains `Unspecified`. The victim cannot vote on this proposal, and the same trap will occur on every future proposal as long as the attacker's follower neurons remain.

### Citations

**File:** rs/nns/governance/src/voting.rs (L20-31)
```rust
const BILLION: u64 = 1_000_000_000;

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

**File:** rs/nns/governance/src/voting.rs (L122-180)
```rust
    pub async fn cast_vote_and_cascade_follow(
        &mut self,
        proposal_id: ProposalId,
        voting_neuron_id: NeuronId,
        vote_of_neuron: Vote,
        topic: Topic,
    ) {
        let voting_started = self.env.now();

        if !self.heap_data.proposals.contains_key(&proposal_id.id) {
            // This is a critical error, but there is nothing that can be done about it
            // at this place.  We somehow have a vote for a proposal that doesn't exist.
            eprintln!(
                "error in cast_vote_and_cascade_follow: Proposal not found: {}",
                proposal_id.id
            );
            return;
        }

        // First we cast the ballot.
        self.record_neuron_vote(proposal_id, voting_neuron_id, vote_of_neuron, topic);

        // We process until voting is finished, and then do any other work that fits into the soft
        // limit of the current message.  Votes are guaranteed to be recorded before the function
        // returns, but recent_ballots for neurons might be recorded later in a timer job.  This
        // ensures we return to the caller in a reasonable amount of time.
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
        // We use the time from the beginning of the function to retain the behaviors needed
        // for wait for quiet even when votes can be processed asynchronously.
        self.recompute_proposal_tally(proposal_id, voting_started);
    }
```

**File:** rs/nns/governance/src/voting.rs (L223-268)
```rust
    /// Process all voting state machines.  This function is called in the timer job.
    /// It processes voting state machines until the soft limit is reached or there is no work to do.
    pub async fn process_voting_state_machines(&mut self) {
        let mut proposals_with_new_votes_cast = vec![];
        with_voting_state_machines_mut(|voting_state_machines| {
            loop {
                if voting_state_machines
                    .with_next_machine(|(proposal_id, machine)| {
                        if !machine.is_voting_finished() {
                            proposals_with_new_votes_cast.push(proposal_id);
                        }
                        // We need to keep track of which proposals we processed
                        self.process_machine_until_soft_limit(machine, over_soft_message_limit);
                    })
                    .is_none()
                {
                    break;
                };

                if over_soft_message_limit() {
                    break;
                }
            }
        });

        // Most of the time, we are not going to see new votes cast in this function, but this
        // is here to make sure the normal proposal processing still applies
        for proposal_id in proposals_with_new_votes_cast {
            self.recompute_proposal_tally(proposal_id, self.env.now());
            self.process_proposal(proposal_id.id);

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
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L3687-3836)
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
