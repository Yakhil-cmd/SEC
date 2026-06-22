### Title
NNS Governance `Follow` Command Allows Duplicate Followee Neuron IDs, Enabling Artificial Vote-Threshold Manipulation - (File: `rs/nns/governance/src/neuron/types.rs`)

---

### Summary

The NNS governance `Follow` command accepts a `Vec<NeuronId>` for followees without enforcing uniqueness. The `would_follow_ballots` function iterates over this Vec and counts yes/no votes, so a duplicate entry causes a single followee's vote to be counted multiple times. This lets any neuron owner bypass the majority-of-followees threshold required to trigger automatic cascade voting.

---

### Finding Description

The `Follow` manage-neuron command stores followees as a plain `Vec<NeuronId>`:

```rust
pub struct Follow {
    pub topic: i32,
    pub followees: Vec<NeuronId>,
}
``` [1](#0-0) 

The `follow()` function in governance validates only that the list is not too long (`MAX_FOLLOWEES_PER_TOPIC`) and that the topic is valid, but performs **no uniqueness check** on the followee IDs: [2](#0-1) 

The newer `SetFollowing` command's `validate_intrinsically()` checks that **topics** are unique but does not check that **followee neuron IDs within a topic** are unique: [3](#0-2) 

The `validate_not_too_many_followees` helper only enforces a count limit, not uniqueness: [4](#0-3) 

The `would_follow_ballots` function, which determines whether a neuron should automatically cast a following vote, iterates over the raw `followees` Vec and tallies yes/no counts:

```rust
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
``` [5](#0-4) 

Because `followees` is a `Vec` and duplicates are never removed, a followee appearing N times has its vote counted N times, while `followees.len()` also grows by N. This skews the majority fraction.

The behavior is explicitly confirmed as accepted by an integration test:

```rust
// neurons can follow the same neuron multiple times
set_followees_on_topic(
    &state_machine,
    &n1,
    &[n2.neuron_id, n2.neuron_id, n2.neuron_id],
    VALID_TOPIC,
);
``` [6](#0-5) 

---

### Impact Explanation

**Governance authorization bug / vote-counting integrity.**

With followees `[A, A, A, B]` (A duplicated 3 times):
- `followees.len() = 4`
- When A votes Yes: `yes = 3`, `3 * 2 = 6 > 4` → follower neuron automatically votes Yes
- B's vote is irrelevant; A alone controls the outcome

With the intended followees `[A, B]` (no duplicates):
- `followees.len() = 2`
- When A votes Yes: `yes = 1`, `1 * 2 = 2 > 2` is **false** → follower neuron does not vote yet
- Both A and B must vote Yes for the follower to cascade

A neuron owner can therefore make their neuron vote automatically based on a single followee's decision, bypassing the majority-of-followees requirement. In NNS governance, where large neurons (e.g., foundation neurons, whale stakers) participate in high-stakes proposals (protocol upgrades, treasury transfers, subnet management), an attacker who controls such a neuron can configure it to auto-vote on any proposal the moment a single chosen followee votes, regardless of what other followees do. This undermines the intended distributed-control semantics of the following mechanism.

---

### Likelihood Explanation

**Medium.** Any neuron controller or hot-key holder can submit a `Follow` manage-neuron command with repeated neuron IDs via a standard ingress message — no privileged access is required. The attack is silent (no on-chain event distinguishes a duplicate-followee list from a normal one) and persistent (the followees list is stored in neuron state). The primary constraint is that the attacker must already control a neuron, which is a low barrier on the NNS.

---

### Recommendation

1. **Enforce uniqueness in `follow()`**: After parsing the `Follow` command, deduplicate the followees list or reject requests containing duplicate neuron IDs:

   ```rust
   let unique_followees: HashSet<u64> = follow_request.followees.iter().map(|f| f.id).collect();
   if unique_followees.len() != follow_request.followees.len() {
       return Err(GovernanceError::new_with_message(
           ErrorType::InvalidCommand,
           "Followees list contains duplicate neuron IDs.",
       ));
   }
   ```

2. **Extend `SetFollowing` validation**: Add a followee-uniqueness check to `validate_not_too_many_followees` (or a new `validate_followees_are_unique` step) in `rs/nns/governance/src/pb/mod.rs`, mirroring what SNS governance already does via `get_duplicate_followee_groups` in `rs/sns/governance/src/following.rs`. [7](#0-6) 

3. **Defensive deduplication in `would_follow_ballots`**: As a defense-in-depth measure, deduplicate the followees slice before counting votes.

---

### Proof of Concept

**Setup**: Neuron N1 is configured to follow `[N2, N2, N2, N3]` on topic `Governance` using the `Follow` manage-neuron command (accepted without error, as confirmed by the integration test).

**Trigger**: N2 votes Yes on a Governance proposal.

**Execution of `would_follow_ballots`**:
- `followees = [N2, N2, N2, N3]`, `followees.len() = 4`
- N2's ballot: `Vote::Yes`
- Loop iteration 1 (N2): `yes = 1`
- Loop iteration 2 (N2): `yes = 2`
- Loop iteration 3 (N2): `yes = 3`
- Loop iteration 4 (N3): N3 has not voted → no change
- Check: `yes * 2 = 6 > 4 = followees.len()` → **returns `Vote::Yes`**

**Result**: N1 automatically votes Yes, even though only 1 out of 2 distinct followees (N2 and N3) has voted. With a legitimate `[N2, N3]` list, N2 voting alone would yield `1 * 2 = 2 > 2` → **false** → N1 would not auto-vote.

The attacker (N1's controller) has effectively reduced the threshold for N1's automatic vote from a true majority of distinct followees to a single chosen followee, by padding the followees list with duplicates. [8](#0-7) [9](#0-8) [4](#0-3)

### Citations

**File:** rs/nns/governance/api/src/types.rs (L950-954)
```rust
    pub struct Follow {
        /// Topic UNSPECIFIED means add following for the 'catch all'.
        pub topic: i32,
        pub followees: Vec<NeuronId>,
    }
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

**File:** rs/nns/governance/src/pb/mod.rs (L92-97)
```rust
    pub fn validate_intrinsically(&self) -> Result<(), GovernanceError> {
        self.validate_topics_are_unique()?;
        self.validate_not_too_many_followees()?;

        Ok(())
    }
```

**File:** rs/nns/governance/src/pb/mod.rs (L127-144)
```rust
    fn validate_not_too_many_followees(&self) -> Result<(), GovernanceError> {
        for followees_for_topic in &self.topic_following {
            let FolloweesForTopic { followees, topic } = followees_for_topic;

            if followees.len() > MAX_FOLLOWEES_PER_TOPIC {
                return Err(GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    format!(
                        "Too many followees (on topic {:?}): {} followees vs. at most {} is allowed.",
                        topic.map(Topic::try_from),
                        followees.len(),
                        MAX_FOLLOWEES_PER_TOPIC,
                    ),
                ));
            }
        }

        Ok(())
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

**File:** rs/sns/governance/src/following.rs (L341-345)
```rust
        let duplicate_neuron_ids = get_duplicate_followee_groups(&followees);

        if !duplicate_neuron_ids.is_empty() {
            return Err(Self::Error::DuplicateFolloweeNeuronId(duplicate_neuron_ids));
        }
```
