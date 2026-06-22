### Title
Duplicate Followee Neuron IDs in NNS Governance `Follow` Command Skew Cascade Voting Tally - (File: rs/nns/governance/src/neuron/types.rs)

### Summary
The NNS Governance `manage_neuron::Follow` command accepts a `Vec<NeuronId>` followees list without checking for duplicate entries. The `would_follow_ballots` function iterates over this raw vector and counts each occurrence of a followee's vote, using `followees.len()` (including duplicates) as the majority denominator. A neuron owner can therefore inflate the effective weight of a single followee's vote by listing it multiple times, forcing their neuron to vote Yes even when the set of unique followees is split 50/50 — a result that would otherwise produce a No vote. Because NNS governance uses a cascade (BFS) mechanism, this skewed vote propagates to every neuron that follows the manipulated neuron.

### Finding Description
The `follow` function in `rs/nns/governance/src/governance.rs` validates only that the followees list does not exceed `MAX_FOLLOWEES_PER_TOPIC` in raw length; it performs no deduplication. [1](#0-0) 

The stored `Followees { followees: Vec<NeuronId> }` is therefore a plain vector that may contain repeated IDs. When cascade voting fires, `would_follow_ballots` iterates over this vector directly: [2](#0-1) 

Both the yes/no counters and the denominator `followees.len()` include every duplicate occurrence. With followees `[A, A, B]` where A=Yes and B=No: `yes=2, no=1, len=3` → `2×2=4 > 3` → **Vote::Yes**. With the deduplicated list `[A, B]`: `yes=1, no=1, len=2` → `1×2=2 ≮ 2` → **Vote::No**. The same root cause exists in SNS Governance's legacy `Follow` path: [3](#0-2) [4](#0-3) 

Notably, the newer SNS `SetFollowing` command **does** reject duplicates: [5](#0-4) 

An integration test explicitly documents that the NNS `Follow` path currently permits duplicates: [6](#0-5) 

### Impact Explanation
A neuron owner submits a `manage_neuron::Follow` ingress message listing the same followee ID multiple times (e.g., `[A, A, B]`). When A votes Yes and B votes No on any proposal, the follower's ballot is automatically cast Yes instead of No. Because NNS governance cascades votes through the follow graph, every neuron that transitively follows the manipulated neuron is also affected. On high-stakes proposals (e.g., NNS upgrades, treasury transfers), this allows a single neuron owner to misrepresent their following configuration and tip the cascade outcome in their favour even when the genuine unique followees are evenly split. The impact is **governance outcome manipulation** rather than direct token theft, but on the NNS this affects protocol-level decisions.

### Likelihood Explanation
The attack requires only a standard `manage_neuron` ingress call — no privileged role, no key compromise, no threshold attack. Any neuron owner (an unprivileged ingress sender) can submit the malformed followees list at any time. The `MAX_FOLLOWEES_PER_TOPIC` check is the only guard, and it counts raw list length including duplicates, so the attacker can fill all slots with a single repeated ID. The existing integration test confirms the path is reachable and currently succeeds.

### Recommendation
In the `follow` function, deduplicate the incoming `followees` vector (or reject the request if duplicates are detected) before storing it, mirroring the validation already present in `ValidatedFolloweesForTopic::try_from` for the SNS `SetFollowing` path:

```rust
// Before storing, check for duplicates
let unique: HashSet<_> = follow_request.followees.iter().collect();
if unique.len() != follow_request.followees.len() {
    return Err(GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "Followees list must not contain duplicate neuron IDs.",
    ));
}
```

Apply the same fix to the SNS legacy `Follow` handler in `rs/sns/governance/src/governance.rs`.

### Proof of Concept
1. Neuron N1 (attacker) calls `manage_neuron::Follow` with `followees = [A, A, B]` on any non-ManageNeuron topic.
2. The `follow` function stores the list as-is (no duplicate check).
3. A proposal is created; N1 receives a blank ballot.
4. Followee A votes Yes; followee B votes No.
5. `would_follow_ballots` is called for N1: `yes=2, no=1, len=3` → `2×2=4 > 3` → N1's ballot is automatically cast **Yes**.
6. Without duplicates (`[A, B]`): `yes=1, no=1, len=2` → tie → N1's ballot would be cast **No**.
7. Any neuron that follows N1 inherits the Yes vote via the BFS cascade, amplifying the effect across the follow graph. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5718-5791)
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

        self.with_neuron_mut(id, |neuron| {
            neuron.followees = new_neuron_followees;

            // Changing followees may change the voting power, so refresh it.
            neuron.refresh_voting_power(now_seconds);
        })?;

        Ok(())
    }
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

**File:** rs/sns/governance/src/neuron.rs (L347-391)
```rust
        let mut yes: usize = 0;
        let mut no: usize = 0;
        for neuron_id in &followees {
            let Some(ballot) = ballots.get(&neuron_id.to_string()) else {
                continue;
            };

            let followee_vote = Vote::try_from(ballot.vote);

            debug_assert!(
                followee_vote.is_ok(),
                "Cannot convert ballot vote to Vote for {ballot:?}"
            );

            let Ok(followee_vote) = Vote::try_from(ballot.vote) else {
                log!(
                    ERROR,
                    "Skipping followee neuron {} with an invalid vote {} for proposal {}",
                    neuron_id,
                    ballot.vote,
                    proposal_id.id,
                );
                continue;
            };

            if followee_vote == Vote::Yes {
                yes += 1;
            } else if followee_vote == Vote::No {
                no += 1;
            }
        }

        // Step 3: Use vote counts to decide which Vote option to return.

        // If a majority of followees voted Yes, return Yes.
        if yes.saturating_mul(2) > followees.len() {
            return Vote::Yes;
        }
        // If a majority for Yes can never be achieved, return No.
        if no.saturating_mul(2) >= followees.len() {
            return Vote::No;
        }
        // Otherwise, we are still open to going either way.
        Vote::Unspecified
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

**File:** rs/sns/governance/src/following.rs (L341-345)
```rust
        let duplicate_neuron_ids = get_duplicate_followee_groups(&followees);

        if !duplicate_neuron_ids.is_empty() {
            return Err(Self::Error::DuplicateFolloweeNeuronId(duplicate_neuron_ids));
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
