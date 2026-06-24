### Title
Duplicate Followees in NNS Governance `Follow` Command Skew Automatic Voting Majority Calculation - (File: rs/nns/governance/src/governance.rs)

### Summary

The NNS governance `Follow` command (`manage_neuron`) accepts a list of followee neuron IDs without validating for duplicates. The `would_follow_ballots` function iterates over the raw followees list and counts each entry's vote independently, so a duplicate followee's vote is counted multiple times. This breaks the intended invariant that automatic following requires a majority of *distinct* followees, and is reachable by any unprivileged neuron owner via ingress.

### Finding Description

The `follow` function in `rs/nns/governance/src/governance.rs` validates the followees list only for length and topic validity: [1](#0-0) 

No deduplication or uniqueness check is performed. The followees are stored as a raw `Vec<NeuronId>` in the neuron's state: [2](#0-1) 

The `would_follow_ballots` function in `rs/nns/governance/src/neuron/types.rs` then iterates over this list and counts each entry's vote independently: [3](#0-2) 

Because `followees.len()` includes duplicates, a single followee listed N times contributes N votes to the yes/no count while also inflating the denominator. This allows a neuron owner to make a single followee achieve a supermajority by listing it multiple times, while other listed followees become irrelevant to the outcome.

The codebase explicitly acknowledges this as a known state inconsistency: [4](#0-3) 

An integration test confirms the behavior is accepted at the protocol level: [5](#0-4) 

### Impact Explanation

**Voting majority manipulation**: With followees `[A, A, A, B]` (3 duplicates of A, 1 of B), `followees.len() = 4`. If A votes Yes, `yes = 3` (counted three times), and `3 * 2 = 6 > 4` → the follower votes Yes. Neuron B's vote is irrelevant. Without duplicates, with `[A, B]`, if only A votes Yes, `yes = 1`, `1 * 2 = 2` is not `> 2` → the follower waits. The duplicate pattern allows a neuron owner to make their neuron follow a single neuron with an artificially inflated weight, bypassing the intended "majority of distinct followees" semantics.

**Index inconsistency**: The following index (used for vote cascade BFS) deduplicates entries, but the primary followees data does not. This creates a permanent divergence between the index and primary state: [6](#0-5) 

**Memory waste**: With `MAX_FOLLOWEES_PER_TOPIC = 15`, a neuron owner can store 15 copies of the same neuron ID per topic, wasting stable memory in the governance canister across all neurons and topics.

### Likelihood Explanation

Any neuron owner can submit a `ManageNeuron { command: Follow { followees: [id, id, id, ...] } }` ingress message. No special privilege is required. The governance canister accepts and stores the duplicates without error. The integration test at `rs/nns/integration_tests/src/neuron_following.rs:189` confirms this path is reachable on mainnet.

### Recommendation

**Short term**: In the `follow` function, deduplicate the followees list before storing it, or reject requests containing duplicate neuron IDs with an `InvalidCommand` error. The check should be placed alongside the existing length check: [1](#0-0) 

**Long term**: Add a property-based test (using `proptest` or similar) that verifies the following invariant: for any followees list, the result of `would_follow_ballots` is identical to the result computed over the deduplicated list. This would catch regressions in the majority calculation logic.

### Proof of Concept

1. Obtain a neuron (stake ICP and claim via `ManageNeuron::ClaimOrRefresh`).
2. Submit a `ManageNeuron` ingress message with `Command::Follow { topic: <any valid topic>, followees: [N2, N2, N2, N3] }` where N2 and N3 are valid neuron IDs.
3. Observe that the governance canister accepts the request and stores all four entries (including three duplicates of N2).
4. When N2 votes Yes on a proposal, the follower's `would_follow_ballots` computes `yes = 3`, `followees.len() = 4`, `3 * 2 = 6 > 4` → follower automatically votes Yes, regardless of N3's vote.
5. Contrast with `followees: [N2, N3]`: if only N2 votes Yes, `yes = 1`, `1 * 2 = 2` is not `> 2` → follower does not automatically vote until N3 also votes.

The root cause is in `rs/nns/governance/src/governance.rs` (`follow` function, no uniqueness check) and `rs/nns/governance/src/neuron/types.rs` (`would_follow_ballots`, iterates raw list with duplicates). [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5718-5760)
```rust
    fn follow(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        follow_request: &manage_neuron::Follow,
    ) -> Result<(), GovernanceError> {
        // Find the neuron to modify.
        let (is_neuron_controlled_by_caller, is_caller_authorized_to_vote) =
            self.with_neuron(id, |neuron| {
                (
                    neuron.is_controlled_by(caller),
                    neuron.is_authorized_to_vote(caller),
                )
            })?;

        // Only the controller, or a proposal (which passes the controller as the
        // caller), can change the followees for the ManageNeuron topic.
        if follow_request.topic() == Topic::NeuronManagement && !is_neuron_controlled_by_caller {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                "Caller is not authorized to manage following of neuron for the ManageNeuron topic.",
            ));
        } else {
            // Check that the caller is authorized, i.e., either the
            // controller or a registered hot key.
            if !is_caller_authorized_to_vote {
                return Err(GovernanceError::new_with_message(
                    ErrorType::NotAuthorized,
                    "Caller is not authorized to manage following of neuron.",
                ));
            }
        }

        // Check that the list of followees is not too
        // long. Allowing neurons to follow too many neurons
        // allows a memory exhaustion attack on the neurons
        // canister.
        if follow_request.followees.len() > MAX_FOLLOWEES_PER_TOPIC {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "Too many followees.",
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L5776-5781)
```rust
                topic as i32,
                Followees {
                    followees: follow_request.followees.clone(),
                },
            )
        })??;
```

**File:** rs/nns/governance/src/neuron/types.rs (L462-504)
```rust
    pub(crate) fn would_follow_ballots(
        &self,
        topic: Topic,
        ballots: &HashMap<u64, Ballot>,
    ) -> Vote {
        // Compute the list of followees for this topic. If no
        // following is specified for the topic, use the followees
        // from the 'Unspecified' topic.
        if let Some(followees) = self
            .followees
            .get(&(topic as i32))
            .or_else(|| self.followees.get(&(Topic::Unspecified as i32)))
            // extract plain vector from 'Followees' proto
            .map(|x| &x.followees)
        {
            // If, for some reason, a list of followees is specified
            // but empty (this is not normal), don't vote 'no', as
            // would be the natural result of the algorithm below, but
            // instead don't cast a vote.
            if followees.is_empty() {
                return Vote::Unspecified;
            }
            let mut yes: usize = 0;
            let mut no: usize = 0;
            for f in followees.iter() {
                if let Some(f_vote) = ballots.get(&f.id) {
                    if f_vote.vote == (Vote::Yes as i32) {
                        yes = yes.saturating_add(1);
                    } else if f_vote.vote == (Vote::No as i32) {
                        no = no.saturating_add(1);
                    }
                }
            }
            if yes.saturating_mul(2_usize) > followees.len() {
                return Vote::Yes;
            }
            if no.saturating_mul(2_usize) >= followees.len() {
                return Vote::No;
            }
        }
        // No followees specified.
        Vote::Unspecified
    }
```

**File:** rs/nns/governance/src/neuron_data_validation.rs (L526-541)
```rust
    fn validate_cardinalities(_neuron_store: &NeuronStore) -> Option<ValidationIssue> {
        let cardinality_primary =
            with_stable_neuron_store(|stable_neuron_store| stable_neuron_store.lens().followees);
        let cardinality_index =
            with_stable_neuron_indexes(|indexes| indexes.following().num_entries()) as u64;
        // Because followees can have duplicates, the primary data might have larger cardinality
        // than the index. Therefore we only report an issue when index size is larger than primary.
        if cardinality_primary < cardinality_index {
            Some(ValidationIssue::FollowingIndexCardinalityMismatch {
                primary: cardinality_primary,
                index: cardinality_index,
            })
        } else {
            None
        }
    }
```

**File:** rs/nns/integration_tests/src/neuron_following.rs (L189-203)
```rust
#[test]
fn follow_same_neuron_multiple_times() {
    let state_machine = setup_state_machine_with_nns_canisters();

    let n1 = get_neuron_1();
    let n2 = get_neuron_2();

    // neurons can follow the same neuron multiple times
    set_followees_on_topic(
        &state_machine,
        &n1,
        &[n2.neuron_id, n2.neuron_id, n2.neuron_id],
        VALID_TOPIC,
    );
}
```
