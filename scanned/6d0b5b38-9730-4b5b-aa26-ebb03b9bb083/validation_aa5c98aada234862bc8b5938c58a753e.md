### Title
Duplicate Followee Neuron IDs Accepted Without Deduplication Skews NNS Governance Vote-Cascade Majority Threshold - (File: rs/nns/governance/src/governance.rs)

---

### Summary

The NNS Governance canister's `follow` function accepts a `followees` list containing duplicate `NeuronId` entries without any deduplication check. The `would_follow_ballots` function then iterates over this raw list and counts votes against `followees.len()` (the inflated denominator). A neuron controller can deliberately supply repeated followee IDs to lower the effective majority threshold required for their neuron to cascade-vote, causing it to follow a single neuron's vote even when that neuron does not hold a true majority among the intended followees.

---

### Finding Description

**Entry point — `follow` in `rs/nns/governance/src/governance.rs`:**

The `follow` function validates authorization, topic validity, and that the list length does not exceed `MAX_FOLLOWEES_PER_TOPIC` (15). It does **not** check for duplicate `NeuronId` values in the supplied list. [1](#0-0) 

The raw list is stored verbatim into the neuron's `followees` map: [2](#0-1) 

This is confirmed by an integration test that explicitly demonstrates the behavior is accepted without error: [3](#0-2) 

**Counting path — `would_follow_ballots` in `rs/nns/governance/src/neuron/types.rs`:**

During vote cascade, `would_follow_ballots` iterates over the raw (potentially duplicate-containing) followees list and computes majority against `followees.len()`: [4](#0-3) 

Because `followees.len()` includes duplicates, a repeated entry inflates the denominator while simultaneously inflating the `yes` or `no` counter for the duplicated neuron. This distorts the majority calculation.

**Concrete arithmetic example:**

- Intended followees: `[A, B, C]` — A votes Yes → `yes=1`, `len=3` → `1×2=2 < 3` → `Vote::Unspecified` (no cascade).
- Exploited followees: `[A, A, A, B, C]` (within the 15-entry limit) — A votes Yes → `yes=3`, `len=5` → `3×2=6 > 5` → `Vote::Yes` (cascade fires).

The neuron controller has effectively reduced the majority threshold from 2/3 to 3/5 by repeating A twice.

The same `would_follow_ballots` logic is invoked via `neuron_would_follow_ballots` in the NNS voting state machine: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

A neuron controller (or authorized hot key) can craft a `Follow` ingress message with repeated `NeuronId` entries to make their neuron cascade-vote on NNS proposals based on a single followee's vote, even when that followee does not hold a true majority. If the manipulated neuron is a large or named neuron that many other neurons follow, the cascade propagates further, potentially swaying the outcome of NNS governance proposals — including critical ones such as subnet upgrades, replica version elections, or treasury transfers — in a direction that does not reflect the genuine consensus of the intended followee set.

---

### Likelihood Explanation

Any neuron controller or registered hot key can submit a `manage_neuron` `Follow` command with duplicate IDs via a standard ingress call to the NNS governance canister. No privileged access, no threshold corruption, and no social engineering is required. The manipulation is subtle (the followee list is not prominently displayed in governance UIs) and is not rejected by the canister. The `MAX_FOLLOWEES_PER_TOPIC = 15` limit still applies to the total count including duplicates, so an attacker can repeat a single followee up to 14 times alongside one other followee, achieving an extreme skew. [7](#0-6) 

---

### Recommendation

**Short term:** In the `follow` function, deduplicate the incoming `followees` list before storing it, or reject the request if duplicates are detected:

```rust
// After the length check, before storing:
let mut seen = HashSet::new();
for f in &follow_request.followees {
    if !seen.insert(f.id) {
        return Err(GovernanceError::new_with_message(
            ErrorType::InvalidCommand,
            "Duplicate followee neuron ID.",
        ));
    }
}
```

**Long term:** Represent the followees collection as a `HashSet<NeuronId>` or `BTreeSet<NeuronId>` at the type level so that uniqueness is structurally enforced, mirroring the approach already taken in the SNS governance `ValidatedFolloweesForTopic` which uses `BTreeSet<ValidatedFollowee>` and explicitly rejects duplicates. [8](#0-7) [9](#0-8) 

---

### Proof of Concept

1. Obtain a neuron with controller principal `P` and neuron ID `N`.
2. Identify a target followee neuron `A` whose vote you want `N` to mirror even without a true majority.
3. Submit a `manage_neuron` ingress call from `P`:
   ```
   ManageNeuron {
     neuron_id_or_subaccount: NeuronId(N),
     command: Follow {
       topic: <any non-NeuronManagement topic>,
       followees: [A, A, A, B, C],   // A repeated 3×; total = 5 ≤ 15
     }
   }
   ```
4. The call succeeds (confirmed by the integration test `follow_same_neuron_multiple_times`).
5. When neuron `A` votes Yes on any proposal of that topic, `would_follow_ballots` computes `yes=3`, `len=5`, `3×2=6 > 5` → neuron `N` cascade-votes Yes, even though only 1 of the 3 distinct followees voted.
6. Without the duplicate trick (`[A, B, C]`), `yes=1`, `len=3`, `1×2=2 < 3` → neuron `N` does **not** cascade-vote. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/nns/governance/src/governance.rs (L212-213)
```rust
/// The maximum number of followees each neuron can establish for each topic.
pub const MAX_FOLLOWEES_PER_TOPIC: usize = 15;
```

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

**File:** rs/nns/governance/src/governance.rs (L5771-5784)
```rust
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
```

**File:** rs/nns/integration_tests/src/neuron_following.rs (L189-202)
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

**File:** rs/nns/governance/src/neuron_store.rs (L774-786)
```rust
    pub fn neuron_would_follow_ballots(
        &self,
        neuron_id: NeuronId,
        topic: Topic,
        ballots: &HashMap<u64, Ballot>,
    ) -> Result<Vote, NeuronStoreError> {
        let needed_sections = NeuronSections {
            followees: true,
            ..NeuronSections::NONE
        };
        self.with_neuron_sections(&neuron_id, needed_sections, |neuron| {
            neuron.would_follow_ballots(topic, ballots)
        })
```

**File:** rs/nns/governance/src/voting.rs (L527-546)
```rust
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
```

**File:** rs/sns/governance/src/following.rs (L39-46)
```rust
#[derive(Debug, PartialEq)]
pub(crate) struct ValidatedFolloweesForTopic {
    /// If this is empty, it means that the neuron is not following any other neurons on this topic.
    /// An empty set is used also to unset the followees for a given topic.
    pub followees: BTreeSet<ValidatedFollowee>,

    pub topic: Topic,
}
```

**File:** rs/sns/governance/src/following.rs (L310-348)
```rust
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
