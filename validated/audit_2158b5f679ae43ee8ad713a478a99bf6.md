### Title
Duplicate Followee Neuron IDs in NNS `Follow` Command Distort Vote-Propagation Majority Calculation - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS governance `follow` function stores a caller-supplied `Vec<NeuronId>` of followees without deduplication. The `would_follow_ballots` function counts each raw list entry independently, so duplicate entries inflate both the yes/no vote tallies and the denominator `followees.len()`, distorting the majority threshold used for automatic vote propagation. An unprivileged neuron controller can exploit this to flip their neuron's cascaded vote outcome.

---

### Finding Description

The `follow` function in `rs/nns/governance/src/governance.rs` validates only that the followee list does not exceed `MAX_FOLLOWEES_PER_TOPIC` in length, then stores the caller-supplied list verbatim as a `Vec<NeuronId>` inside a `Followees` proto: [1](#0-0) 

No deduplication or uniqueness check is performed on `follow_request.followees`. The integration test `follow_same_neuron_multiple_times` in `rs/nns/integration_tests/src/neuron_following.rs` explicitly confirms the system accepts and stores `[n2.neuron_id, n2.neuron_id, n2.neuron_id]`: [2](#0-1) 

The comment on line 196 reads: *"neurons can follow the same neuron multiple times"*. The neuron data validator test in `rs/nns/governance/src/neuron_data_validation.rs` also explicitly notes: *"Both followees and principals (controller is a hot key) have duplicates since we do allow it at this time."* [3](#0-2) 

During vote propagation, `would_follow_ballots` in `rs/nns/governance/src/neuron/types.rs` iterates over the raw `followees` Vec and counts each entry independently: [4](#0-3) 

The majority check is:
- `yes * 2 > followees.len()` → Vote::Yes
- `no * 2 >= followees.len()` → Vote::No

Both the numerator (yes/no counts) and the denominator (`followees.len()`) are inflated by duplicates, distorting the threshold. This function is called from the voting state machine in `rs/nns/governance/src/voting.rs`: [5](#0-4) 

**Contrast with SNS**: The newer SNS `SetFollowing` path in `rs/sns/governance/src/following.rs` explicitly validates for duplicate followee neuron IDs and returns a `DuplicateFolloweeNeuronId` error: [6](#0-5) 

The NNS `follow` path has no equivalent guard.

---

### Impact Explanation

A neuron controller can manipulate their neuron's automatic vote following to flip the cascaded vote outcome. Concrete example:

- Neuron A follows `[B, B, B, C]` on topic X (4 entries, 2 unique)
- B votes Yes, C votes No
- `would_follow_ballots`: `yes=3`, `no=1`, `len=4` → `3*2=6 > 4` → **Vote::Yes**
- With deduplicated list `[B, C]`: `yes=1`, `no=1`, `len=2` → `1*2=2 > 2` → false; `1*2=2 >= 2` → **Vote::No**

The duplicate manipulation flips A's cascaded vote from No to Yes. Since NNS proposal outcomes depend on the aggregate of all cascaded ballots, this distorts governance decisions. The attacker amplifies the weight of a chosen followee (e.g., one they control) to override the natural majority among their followees.

---

### Likelihood Explanation

Any neuron controller — an unprivileged ingress sender — can call `manage_neuron` with a `Follow` command containing duplicate neuron IDs. No special privilege, admin key, or threshold corruption is required. The system explicitly accepts such requests (confirmed by the integration test). The `MAX_FOLLOWEES_PER_TOPIC` limit does not prevent this because duplicates count toward the limit, but an attacker can still construct a list like `[B, B, ..., C]` within the limit to amplify B's weight.

---

### Recommendation

In the `follow` function in `rs/nns/governance/src/governance.rs`, deduplicate `follow_request.followees` before storing (e.g., collect into a `HashSet<NeuronId>` and convert back), or reject the request with `ErrorType::InvalidCommand` if any duplicate neuron IDs are detected. This mirrors the guard already present in the SNS `SetFollowing` path (`rs/sns/governance/src/following.rs`, `get_duplicate_followee_groups`).

---

### Proof of Concept

1. Neuron A's controller submits:
   ```
   manage_neuron(Follow { topic: NetworkEconomics, followees: [B, B, B, C] })
   ```
2. The governance canister stores `[B, B, B, C]` as A's followees for `NetworkEconomics`.
3. B votes Yes on a proposal; C votes No.
4. `would_follow_ballots` is called for A during vote cascade:
   - Iterates `[B, B, B, C]`: `yes += 1` (B), `yes += 1` (B), `yes += 1` (B), `no += 1` (C)
   - `yes=3`, `no=1`, `followees.len()=4`
   - `3*2=6 > 4` → returns `Vote::Yes`
5. A's ballot is set to Yes via `cast_vote` in the voting state machine.
6. **Expected behavior** (deduplicated `[B, C]`): `yes=1`, `no=1`, `len=2` → tie → `Vote::No`.
7. The duplicate manipulation flipped A's cascaded vote from No to Yes, affecting the NNS proposal tally. [7](#0-6) [4](#0-3) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5751-5781)
```rust
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

**File:** rs/nns/governance/src/neuron_data_validation.rs (L782-834)
```rust
    #[test]
    fn test_validator_valid() {
        // Both followees and principals (controller is a hot key) have duplicates since we do allow
        // it at this time.
        let neuron = NeuronBuilder::new(
            NeuronId { id: 1 },
            Subaccount::try_from([1_u8; 32].as_ref()).unwrap(),
            PrincipalId::new_user_test_id(1),
            DissolveStateAndAge::DissolvingOrDissolved {
                when_dissolved_timestamp_seconds: 1,
            },
            123_456_789,
        )
        .with_hot_keys(vec![
            PrincipalId::new_user_test_id(2),
            PrincipalId::new_user_test_id(3),
            PrincipalId::new_user_test_id(1),
        ])
        .with_followees(hashmap! {
            1 => Followees{
                followees: vec![
                    NeuronId { id: 2 },
                    NeuronId { id: 4 },
                    NeuronId { id: 3 },
                    NeuronId { id: 2 },
                ],
            },
        })
        .with_maturity_disbursements_in_progress(vec![
            MaturityDisbursement {
                finalize_disbursement_timestamp_seconds: 1,
                ..Default::default()
            },
            MaturityDisbursement {
                finalize_disbursement_timestamp_seconds: 1,
                ..Default::default()
            },
            MaturityDisbursement {
                finalize_disbursement_timestamp_seconds: 2,
                ..Default::default()
            },
        ])
        .build();

        let neuron_store = NeuronStore::new(btreemap! {neuron.id().id => neuron});
        let mut validator = NeuronDataValidator::new();
        let mut now = 1;
        while validator.maybe_validate(now, &neuron_store) {
            now += 1;
        }
        let summary = validator.summary();
        assert_eq!(summary.current_issues_summary.unwrap().issue_groups, vec![]);
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L484-500)
```rust
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
```

**File:** rs/nns/governance/src/voting.rs (L478-503)
```rust
    fn cast_vote(&mut self, ballots: &mut HashMap<u64, Ballot>, neuron_id: NeuronId, vote: Vote) {
        // There is no action to take with unspecfiied votes, so we early return.  It is
        // a legitimate argument in the context of continue_processing, but it simply means
        // that no vote is cast, and therefore there is no followup work to do.
        // This condition is also important to ensure that the state machine always terminates
        // even if an Unspecified vote is somehow cast manually.
        if vote == Vote::Unspecified {
            return;
        }

        if let Some(ballot) = ballots.get_mut(&neuron_id.id) {
            // The following conditional is CRITICAL, as it prevents a neuron's vote from
            // being overwritten by a later vote. This is important because otherwse
            // a cyclic voting graph is possible, which could result in never finishing voting.
            if ballot.vote == Vote::Unspecified as i32 {
                // Cast vote in ballot
                ballot.vote = vote as i32;
                // record the votes that have been cast, to log
                self.recent_neuron_ballots_to_record.insert(neuron_id, vote);

                // Do not check followers for NeuronManagement topic
                if self.topic != NeuronManagement {
                    self.neurons_to_check_followers.insert(neuron_id);
                }
            }
        }
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

**File:** rs/sns/governance/src/following.rs (L295-348)
```rust
#[derive(Error, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) enum FolloweesForTopicValidationError {
    #[error("topic must be set to one from SnsGov.list_topics()")]
    UnspecifiedTopic,

    #[error("a neuron can only follow up to {} other neurons on a given topic (requested {})", MAX_FOLLOWEES_PER_TOPIC, .0)]
    TooManyFollowees(usize),

    #[error("some followees were not specified correctly: {:?}", .0)]
    FolloweeValidationError(Vec<FolloweeValidationError>),

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
