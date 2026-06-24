Audit Report

## Title
SNS Governance `cast_vote_and_cascade_follow` Performs Unbounded Synchronous BFS Over Follower Graph, Enabling Instruction-Exhaustion DoS Against Any Voting Neuron - (File: `rs/sns/governance/src/governance.rs`)

## Summary

The SNS governance canister's `cast_vote_and_cascade_follow` is a plain synchronous function that performs an unbounded BFS over all neurons following the voting neuron, with no instruction-limit checks at any point in the traversal. Because the `follow` endpoint enforces only a per-neuron followee cap (`max_followees_per_function`) and places no cap on the number of neurons that may follow a given neuron, an attacker who stakes K cheap SNS neurons and sets each to follow a victim neuron can cause every subsequent `register_vote` call by that victim to trap at the IC's 5-billion-instruction per-message limit, permanently preventing the victim from recording any ballot.

## Finding Description

**Root cause — unbounded synchronous BFS:**
`cast_vote_and_cascade_follow` at [1](#0-0)  is declared `fn` (not `async`). The outer BFS loop at [2](#0-1)  and the inner follower-collection loop at [3](#0-2)  both iterate over attacker-controlled data. There is no call to `ic_cdk::api::instruction_counter()`, no `noop_self_call_if_over_instructions`, and no soft/hard limit constant anywhere in `rs/sns/governance/src/governance.rs` — confirmed by the absence of any match for those identifiers in the file.

**Root cause — unbounded fan-in:**
The `follow` function at [4](#0-3)  checks only that the *follower's* followee list does not exceed `max_followees_per_function`. The reverse index update at [5](#0-4)  inserts the follower into `all_followers` with no cap on the size of that set. Any number of neurons may follow a single target neuron.

**Contrast with NNS governance:**
NNS governance defines `SOFT_VOTING_INSTRUCTIONS_LIMIT` (1 billion) and `HARD_VOTING_INSTRUCTIONS_LIMIT` (750 billion) at [6](#0-5) , and its `cast_vote_and_cascade_follow` is `async`, calling `noop_self_call_if_over_instructions` after each BFS tier at [7](#0-6) . SNS governance has no equivalent.

**Exploit path:**
1. Attacker stakes K SNS neurons via repeated `ClaimOrRefresh`.
2. For each attacker neuron, calls `manage_neuron → Follow { function_id, followees: [victim_neuron_id] }`. The `follow` function accepts each call because the attacker neuron's followee list has length 1, well within `max_followees_per_function`.
3. Victim neuron calls `manage_neuron → RegisterVote { proposal_id, vote: Yes }`.
4. SNS governance calls `cast_vote_and_cascade_follow` synchronously. The BFS at [8](#0-7)  iterates over all K follower neurons, performing a `neurons.get(...)` and `vote_from_ballots_following(...)` per follower on a `BTreeMap`.
5. With K large enough, the message exhausts 5 billion instructions and traps. All state changes — including the victim's own ballot — are rolled back. The victim's vote is never recorded.

## Impact Explanation

This is a **High** severity finding. An unprivileged attacker can permanently silence a specific neuron on every future proposal in an SNS DAO, constituting a concrete application-level DoS against SNS governance with direct harm to neuron holders and the DAO's ability to function. This matches the allowed impact: *"Application/platform-level DoS... or SNS... security impact with concrete user or protocol harm."*

## Likelihood Explanation

Any principal can stake SNS tokens and call `Follow` without special privileges. The only barrier is the economic cost of staking enough tokens to create K neurons. On low-price SNS instances this cost is modest. The attack is persistent: once the follower neurons are registered, every future vote by the victim traps without further attacker action. No victim mistake or social engineering is required.

## Recommendation

1. Convert `cast_vote_and_cascade_follow` in `rs/sns/governance/src/governance.rs` to an `async` function and insert an instruction-limit check (using `ic_cdk::api::instruction_counter()`) after each BFS tier, deferring remaining work to a timer job analogous to `process_voting_state_machines` in NNS governance.
2. Alternatively (or additionally), enforce a maximum fan-in per neuron per function in the `follow` endpoint: reject a `Follow` call if the target neuron already has more than `MAX_FOLLOWERS` followers for that function ID, mirroring the existing `max_followees_per_function` check on the outbound side.

## Proof of Concept

1. Deploy a local SNS instance (PocketIC or `dfx` with SNS).
2. Stake K neurons (e.g., K = 50,000) via repeated `ClaimOrRefresh`; set each to follow a designated victim neuron on function ID 0 via `manage_neuron → Follow`.
3. Create a proposal and have the victim neuron call `manage_neuron → RegisterVote { proposal_id, vote: Yes }`.
4. Observe that the update call traps with an instruction-limit error and the victim's ballot remains `Unspecified`.
5. Confirm the attack is repeatable on every subsequent proposal without any additional attacker action.

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

**File:** rs/sns/governance/src/governance.rs (L4044-4046)
```rust
            for followee in &f.followees {
                let all_followers = cache.entry(followee.to_string()).or_default();
                all_followers.insert(id.clone());
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

**File:** rs/nns/governance/src/voting.rs (L163-175)
```rust
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
```
