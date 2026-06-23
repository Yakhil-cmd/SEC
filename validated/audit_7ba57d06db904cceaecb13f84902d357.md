### Title
Synchronous BFS in SNS `cast_vote_and_cascade_follow` Lacks Instruction-Limit Protection, Enabling Governance Canister DoS — (`rs/sns/governance/src/governance.rs`)

### Summary

The SNS governance `cast_vote_and_cascade_follow` is a **synchronous, non-async function** with no instruction-limit guard. An attacker who controls a hub neuron and N follower neurons (all following the hub) can trigger a single-message BFS cascade over all N followers when the hub votes, potentially exhausting the Wasm instruction budget and trapping the governance canister. The NNS governance has an explicit, well-tested mitigation for this exact scenario; the SNS governance does not.

---

### Finding Description

**SNS governance — no protection (the vulnerable path):**

`cast_vote_and_cascade_follow` in the SNS is a plain synchronous function: [1](#0-0) 

The BFS loop runs to completion in a single call with no instruction checks: [2](#0-1) 

It is called directly (synchronously) from `register_vote`: [3](#0-2) 

**NNS governance — the mitigation that SNS lacks:**

The NNS version defines explicit soft and hard instruction limits: [4](#0-3) 

Its `cast_vote_and_cascade_follow` is `async` and breaks work across messages via `noop_self_call_if_over_instructions`, deferring remaining work to a timer: [5](#0-4) 

The SNS has **none** of this: no soft limit, no hard limit, no self-call, no timer fallback.

---

### Impact Explanation

When the hub neuron votes, `register_vote` calls `cast_vote_and_cascade_follow` synchronously. The BFS iterates over every follower neuron in a single update-call message. Each iteration performs `BTreeMap` lookups, `vote_from_ballots_following` evaluation, and `BTreeSet` insertions. With a large enough star topology, the cumulative instruction count exceeds the IC per-message limit (~5 billion instructions for update calls), causing the canister to **trap**. A trap rolls back state but the canister remains alive; however, repeated triggering (e.g., on every open proposal) can render the governance canister unable to process votes, effectively a non-volumetric DoS of the SNS governance.

---

### Likelihood Explanation

The attacker must:
1. Create N neurons in the target SNS (requires staking governance tokens — an economic barrier).
2. Set each follower neuron to follow the hub via `manage_neuron(Follow)` — a standard, permissionless operation.
3. Wait for or submit a proposal (requires meeting the SNS proposal submission threshold).
4. Vote with the hub neuron.

The economic cost of staking tokens is the primary barrier. The feasibility depends on the SNS's `max_number_of_neurons` limit and token price. For SNS instances with low token prices or high neuron counts, the attack is practical. The NNS team already recognized this risk and added explicit protection; the SNS has not received the same fix.

---

### Recommendation

Port the NNS mitigation to the SNS `cast_vote_and_cascade_follow`:

1. Make the function `async`.
2. Add `SOFT_VOTING_INSTRUCTIONS_LIMIT` and `HARD_VOTING_INSTRUCTIONS_LIMIT` constants.
3. Check `over_soft_message_limit()` inside the BFS loop and call `noop_self_call_if_over_instructions` to break work across messages.
4. Persist in-progress BFS state (e.g., via a `VotingStateMachine`) and drain it in a timer job, mirroring the NNS pattern in `rs/nns/governance/src/voting.rs`. [6](#0-5) 

---

### Proof of Concept

**Call sequence:**

1. Attacker stakes tokens and creates N neurons in the target SNS, all following hub neuron H via `manage_neuron { command: Follow { function_id, followees: [H] } }`.
2. Attacker (or anyone) submits a proposal; all N+1 neurons receive ballots.
3. Attacker calls `manage_neuron { command: RegisterVote { proposal_id, vote: Yes } }` for neuron H.
4. `register_vote` → `cast_vote_and_cascade_follow` → synchronous BFS over N followers in one message.
5. At sufficient N, the canister traps on instruction exhaustion.

**Benchmark sketch (canbench):**

```rust
// Star topology: hub neuron 1, followers 2..=N
for i in 2..=N {
    neurons[i].followees = follow(hub_id);
    ballots[i] = unspecified_ballot();
}
// Measure instructions consumed by:
Governance::cast_vote_and_cascade_follow(
    &proposal_id, &hub_id, Vote::Yes, function_id,
    &function_followee_index, &topic_follower_index,
    &neurons, now, &mut ballots, topic,
);
// Assert instruction_counter() < 5_000_000_000
```

The NNS benches already demonstrate this pattern: [7](#0-6)

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

**File:** rs/sns/governance/src/governance.rs (L3931-3944)
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

        self.process_proposal(proposal_id.id);
```

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

**File:** rs/nns/governance/src/voting.rs (L122-176)
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
```

**File:** rs/nns/governance/src/governance/benches.rs (L331-351)
```rust
fn cast_vote_cascade_helper(strategy: SetUpStrategy, topic: Topic) -> BenchResult {
    let mut rng = ChaCha20Rng::seed_from_u64(0);

    let mut governance = Governance::new(
        Default::default(),
        Arc::new(MockEnvironment::new(Default::default(), 0)),
        Arc::new(StubIcpLedger {}),
        Arc::new(StubCMC {}),
        Box::new(MockRandomness::new()),
    );

    let neuron_id = set_up(strategy, &mut rng, &mut governance, topic);

    let proposal_id = ProposalId { id: 1 };
    bench_fn(|| {
        governance
            .cast_vote_and_cascade_follow(proposal_id, neuron_id.into(), Vote::Yes, topic)
            .now_or_never()
            .unwrap();
    })
}
```
