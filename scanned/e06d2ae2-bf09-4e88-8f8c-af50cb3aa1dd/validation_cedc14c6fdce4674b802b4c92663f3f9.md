### Title
SNS Governance `follow` and `set_following` Do Not Validate Followee Neuron Existence - (File: rs/sns/governance/src/governance.rs)

---

### Summary

The SNS governance `follow` and `set_following` functions accept arbitrary neuron IDs as followees without verifying that those neurons actually exist in the SNS neuron store. Any unprivileged neuron holder can silently configure their neuron to follow non-existent neurons, causing automatic vote propagation to silently fail for all proposals, degrading SNS governance participation without any error feedback.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `follow` function (lines 3962–4054) performs the following checks before storing a follow relationship:

1. The follower neuron exists
2. The caller is authorized (`NeuronPermissionType::Vote`)
3. The followees list does not exceed `max_followees_per_function`
4. The `function_id` is a registered SNS function

It does **not** check whether any of the neuron IDs in `f.followees` actually exist in `self.proto.neurons`. The follow relationship is unconditionally stored:

```rust
neuron.followees.insert(
    f.function_id,
    Followees {
        followees: f.followees.clone(),
    },
);
``` [1](#0-0) 

The same omission exists in `set_following` (lines 4056–4158). The `ValidatedSetFollowing::try_from` validation path in `rs/sns/governance/src/following.rs` only checks topic validity, count limits, duplicate neuron IDs, and alias consistency — it never queries the neuron store to confirm existence. [2](#0-1) 

By contrast, the NNS governance `modify_followees` function explicitly checks each new followee's existence (when `is_neuron_follow_restrictions_enabled()` is true) and returns a `PreconditionFailed` error if any followee does not exist:

```rust
} else {
    invalid_followees = invalid_followees.saturating_add(1);
    error_message.push_str(&format!(
        "{}: The neuron with ID {} does not exist...",
        ...
    ));
}
``` [3](#0-2) 

The NNS integration test `neuron_follow_nonexistent_neuron_fails` (marked `#[should_panic]`) documents that following a nonexistent neuron is expected to fail in NNS — confirming the SNS behavior is inconsistent with the NNS design intent. [4](#0-3) 

---

### Impact Explanation

When a neuron follows non-existent neuron IDs, the `cast_vote_and_cascade_follow` logic in SNS governance will never find a matching followee vote to cascade from. The follower neuron silently fails to vote automatically on any proposal for the configured `function_id`. The neuron owner receives no error at follow-setup time and no notification at proposal time. If a significant portion of SNS voting power is configured to follow non-existent neurons (e.g., after a neuron is dissolved and its ID recycled or simply never existed), governance quorum may become unreachable for critical proposals, causing a governance availability degradation.

---

### Likelihood Explanation

Any unprivileged SNS neuron holder can call `manage_neuron` with a `Follow` or `SetFollowing` command via the public `manage_neuron` update endpoint on the SNS governance canister. [5](#0-4) 

The call dispatches directly to `follow` or `set_following` with no pre-validation of followee existence: [6](#0-5) 

This is reachable by any neuron holder on any deployed SNS instance. The scenario where a user mistakenly enters a wrong neuron ID (typo, stale ID from a dissolved neuron, or a neuron that has not yet been created) is realistic and requires no adversarial intent.

---

### Recommendation

In the SNS `follow` function, after the existing checks, iterate over `f.followees` and verify each `NeuronId` exists in `self.proto.neurons`. Return a `GovernanceError` with `ErrorType::NotFound` for any non-existent followee, mirroring the NNS `modify_followees` behavior.

Apply the same check in `set_following` after `ValidatedSetFollowing::try_from` succeeds, before mutating state.

---

### Proof of Concept

1. Deploy an SNS instance with neuron `A` (controlled by principal `P`).
2. Call `manage_neuron` from principal `P` with:
   ```
   ManageNeuron {
     subaccount: <neuron_A_subaccount>,
     command: Follow {
       function_id: <any_registered_function_id>,
       followees: [NeuronId { id: <bytes_of_nonexistent_neuron> }],
     }
   }
   ```
3. Observe: the call returns `ManageNeuronResponse { command: Follow(FollowResponse {}) }` — success, no error.
4. Submit a proposal for the configured `function_id`.
5. Observe: neuron `A` never casts an automatic vote, even though it is configured to follow. The neuron owner has no indication that the follow relationship is inert. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3962-4054)
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

        if !is_registered_function_id(f.function_id, &self.proto.id_to_nervous_system_functions) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!(
                    "Function with id: {} is not present among the current set of functions.",
                    f.function_id,
                ),
            ));
        }

        // First, remove the current followees for this neuron and
        // this function_id from the neuron's followees.
        if let Some(neuron_followees) = neuron.followees.get(&f.function_id) {
            // If this function_id is not represented in the neuron's followees,
            // there is nothing to be removed.
            if let Some(followee_index) = self.function_followee_index.get_mut(&f.function_id) {
                // We need to remove this neuron as a follower
                // for all followees.
                for followee in &neuron_followees.followees {
                    if let Some(all_followers) = followee_index.get_mut(&followee.to_string()) {
                        all_followers.remove(id);
                    }
                    // Note: we don't check that the
                    // function_followee_index actually contains this
                    // neuron's ID as a follower for all the
                    // followees. This could be a warning, but
                    // it is not actionable.
                }
            }
        }
        if !f.followees.is_empty() {
            // Insert the new list of followees for this function_id in
            // the neuron's followees, removing the old list, which has
            // already been removed from the followee index above.
            neuron.followees.insert(
                f.function_id,
                Followees {
                    followees: f.followees.clone(),
                },
            );
            let cache = self
                .function_followee_index
                .entry(f.function_id)
                .or_default();
            // We need to add this neuron as a follower for
            // all followees.
            for followee in &f.followees {
                let all_followers = cache.entry(followee.to_string()).or_default();
                all_followers.insert(id.clone());
            }
            Ok(())
        } else {
            // This operation clears the neuron's followees for the given function_id.
            neuron.followees.remove(&f.function_id);
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L4056-4091)
```rust
    pub fn set_following(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        set_following: &SetFollowing,
    ) -> Result<(), GovernanceError> {
        let neuron = self.proto.neurons.get_mut(&id.to_string()).ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!("Follower neuron not found: {id}"),
            )
        })?;

        // Check that the caller is authorized to change followers (same authorization
        // as voting required).
        neuron.check_authorized(caller, NeuronPermissionType::Vote)?;

        let mentioned_topics = set_following
            .topic_following
            .iter()
            .filter_map(|followees_for_topic| {
                followees_for_topic
                    .topic
                    .and_then(|topic_id| Topic::try_from(topic_id).ok())
            })
            .collect::<BTreeSet<_>>();

        // First, validate the requested followee modifications - in isolation and then in
        // composition with the neuron's old followees.

        // TODO[NNS1-3708]: Avoid cloning the neuron commands.
        let set_following = ValidatedSetFollowing::try_from(set_following.clone())
            .map_err(|err| GovernanceError::new_with_message(ErrorType::InvalidCommand, err))?;
        let old_topic_followees = neuron.topic_followees.clone();
        let new_topic_followees = TopicFollowees::new(old_topic_followees, set_following)
            .map_err(|err| GovernanceError::new_with_message(ErrorType::InvalidCommand, err))?;
```

**File:** rs/sns/governance/src/governance.rs (L4818-4823)
```rust
            C::Follow(f) => self
                .follow(&neuron_id, caller, f)
                .map(|_| ManageNeuronResponse::follow_response()),
            C::SetFollowing(set_following) => self
                .set_following(&neuron_id, caller, set_following)
                .map(|_| ManageNeuronResponse::set_following_response()),
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

**File:** rs/nns/governance/src/governance.rs (L8445-8452)
```rust
        } else {
            invalid_followees = invalid_followees.saturating_add(1);
            error_message.push_str(&format!(
                            "{}: The neuron with ID {} does not exist. Make sure that you copied the neuron ID correctly.\n",
                            invalid_followees,
                            followee.id
                        ));
        }
```

**File:** rs/nns/integration_tests/src/neuron_following.rs (L141-156)
```rust
#[test]
#[should_panic]
fn neuron_follow_nonexistent_neuron_fails() {
    let state_machine = setup_state_machine_with_nns_canisters();

    let n1 = get_neuron_1();
    let nonexistent_neuron = get_nonexistent_neuron();

    // neurons are allowed to follow nonexistent neurons
    set_followees_on_topic(
        &state_machine,
        &n1,
        &[nonexistent_neuron.neuron_id],
        VALID_TOPIC,
    );
}
```

**File:** rs/sns/governance/canister/canister.rs (L397-408)
```rust
#[update]
async fn manage_neuron(request: ManageNeuron) -> ManageNeuronResponse {
    log!(INFO, "manage_neuron");
    let governance = governance_mut();
    let result = measure_span_async(
        governance.profiling_information,
        "manage_neuron",
        governance.manage_neuron(&sns_gov_pb::ManageNeuron::from(request), &caller()),
    )
    .await;
    ManageNeuronResponse::from(result)
}
```
