### Title
NNS Governance `follow` Stores Duplicate Followees Without Deduplication, Distorting Majority-Vote Propagation - (`rs/nns/governance/src/governance.rs`)

### Summary

The NNS governance `follow` command accepts a list of followee neuron IDs and stores them verbatim, without checking for duplicates. The `would_follow_ballots` function then uses the raw (possibly duplicate-inflated) list length as the denominator for majority calculation. A neuron owner can deliberately submit a followees list with repeated entries to give a single followee disproportionate weight in the majority threshold, overriding the votes of other followees.

### Finding Description

The NNS governance `follow` function at `rs/nns/governance/src/governance.rs` validates only that the followees list does not exceed `MAX_FOLLOWEES_PER_TOPIC` in raw length, then stores the list as-is via `modify_followees`: [1](#0-0) 

No deduplication is performed. The stored followees (including duplicates) are later read by `would_follow_ballots` in `rs/nns/governance/src/neuron/types.rs`: [2](#0-1) 

The majority check divides against `followees.len()`, which counts duplicate entries. If a followee appears `k` times, its vote is counted `k` times in the numerator while the denominator is inflated by the same `k`, allowing that single followee to dominate the majority threshold.

The integration test `follow_same_neuron_multiple_times` in `rs/nns/integration_tests/src/neuron_following.rs` explicitly confirms this is accepted by the system: [3](#0-2) 

By contrast, SNS governance explicitly rejects duplicate followees via `ValidatedFolloweesForTopic::try_from` in `rs/sns/governance/src/following.rs`: [4](#0-3) 

### Impact Explanation

**Distorted majority-vote propagation.** Consider a neuron with followees `[A, A, A, B, C]` (A duplicated twice, `len=5`). If A votes Yes and B, C vote No:
- `yes = 3` (A counted 3×), `no = 2`
- `3 × 2 = 6 > 5` → the follower neuron votes **Yes**

Without duplicates (`[A, B, C]`, `len=3`): `yes=1`, `no=2` → `2×2=4 ≥ 3` → **No**.

A single followee's vote overrides a majority of other followees' votes. This violates the intended "majority of followees" semantics and allows a neuron owner to misrepresent their following configuration (appearing to follow multiple neurons while effectively delegating full control to one). The `MAX_FOLLOWEES_PER_TOPIC` memory-exhaustion guard is also weakened, since all slots can be consumed by a single unique neuron ID. [5](#0-4) 

### Likelihood Explanation

Any NNS neuron owner (an unprivileged ingress sender) can submit a `ManageNeuron { command: Follow { followees: [A, A, A, ...] } }` message. No special privilege, key, or majority is required. The path is fully reachable on mainnet by any neuron controller or authorized hot key. [6](#0-5) 

### Recommendation

Deduplicate the followees list before storing it, analogous to what SNS governance already does. In the NNS `follow` function, after the length check, insert a deduplication step:

```rust
let mut seen = HashSet::new();
let deduped: Vec<NeuronId> = follow_request.followees
    .iter()
    .filter(|f| seen.insert(f.id))
    .cloned()
    .collect();
// use deduped instead of follow_request.followees
```

Alternatively, reject requests containing duplicate followee IDs with an `InvalidCommand` error, matching the SNS governance behavior in `rs/sns/governance/src/following.rs`. [7](#0-6) 

### Proof of Concept

1. Neuron owner calls `ManageNeuron` with `Follow { topic: <any>, followees: [A, A, A, B, C] }` where `A`, `B`, `C` are valid neuron IDs. The call succeeds (only the raw length `5 ≤ MAX_FOLLOWEES_PER_TOPIC` is checked).
2. A proposal is submitted on the given topic.
3. Neuron A votes Yes; neurons B and C vote No.
4. `would_follow_ballots` iterates the stored list `[A, A, A, B, C]`: `yes=3`, `no=2`, `len=5`. Since `3×2=6 > 5`, the follower neuron automatically votes **Yes**, despite a 2-to-1 majority of unique followees voting No.
5. Without duplicates (`[A, B, C]`): `yes=1`, `no=2`, `len=3` → `2×2=4 ≥ 3` → **No** — the correct outcome. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5718-5781)
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

        // Validate topic exists
        let topic = Topic::try_from(follow_request.topic).map_err(|_| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                format!("Not a known topic number. Follow:\n{follow_request:#?}"),
            )
        })?;

        let now_seconds = self.env.now();
        let new_neuron_followees = self.with_neuron(id, |neuron| {
            modify_followees(
                &self.neuron_store,
                neuron,
                &neuron.followees,
                topic as i32,
                Followees {
                    followees: follow_request.followees.clone(),
                },
            )
        })??;
```

**File:** rs/nns/governance/src/neuron/types.rs (L462-503)
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

**File:** rs/sns/governance/src/following.rs (L306-348)
```rust
    #[error("followees on a given topic must have unique neuron IDs, got: {}", fmt_followee_groups(.0))]
    DuplicateFolloweeNeuronId(FolloweeGroups),
}

impl TryFrom<FolloweesForTopic> for ValidatedFolloweesForTopic {
    type Error = FolloweesForTopicValidationError;

    fn try_from(value: FolloweesForTopic) -> Result<Self, Self::Error> {
        let FolloweesForTopic { followees, topic } = value;

        let topic = match topic.map(Topic::try_from) {
            Some(Ok(topic)) if topic != Topic::Unspecified => topic,
            _ => {
                return Err(Self::Error::UnspecifiedTopic);
            }
        };

        if followees.len() > MAX_FOLLOWEES_PER_TOPIC {
            return Err(Self::Error::TooManyFollowees(followees.len()));
        }

        let (followees, errors): (Vec<_>, Vec<_>) =
            followees.into_iter().partition_map(|followee| {
                match ValidatedFollowee::try_from((followee, topic)) {
                    Ok(followee) => Either::Left(followee),
                    Err(err) => Either::Right(err),
                }
            });

        if !errors.is_empty() {
            return Err(Self::Error::FolloweeValidationError(errors));
        }

        let followees = followees.into_iter().collect();

        let duplicate_neuron_ids = get_duplicate_followee_groups(&followees);

        if !duplicate_neuron_ids.is_empty() {
            return Err(Self::Error::DuplicateFolloweeNeuronId(duplicate_neuron_ids));
        }

        Ok(Self { followees, topic })
    }
```
