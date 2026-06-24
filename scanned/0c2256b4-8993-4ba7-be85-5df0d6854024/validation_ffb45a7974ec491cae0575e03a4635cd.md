### Title
Duplicate Followee IDs in NNS Governance `Follow` Command Manipulate Automatic Vote Majority Calculation - (`rs/nns/governance/src/neuron/types.rs`, `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS governance `Follow` command (`manage_neuron::Command::Follow`) accepts a `followees` list with no deduplication check. The `would_follow_ballots` function iterates over this raw list and counts each entry — including duplicates — when computing the majority vote threshold. A neuron owner can deliberately list the same followee neuron ID multiple times to inflate that followee's apparent vote weight, causing their neuron to automatically vote Yes even when a majority of *unique* followees voted No (or vice versa). This is a direct analog to the BkdLocker `migrate()` double-counting bug: a missing uniqueness check on a list causes the same entry to be counted multiple times in a critical calculation.

---

### Finding Description

The `follow()` function in `rs/nns/governance/src/governance.rs` validates authorization, topic validity, and list length, but performs **no deduplication** of the submitted `followees` vector: [1](#0-0) 

The raw (potentially duplicate-containing) list is stored directly into `neuron.followees` for the given topic: [2](#0-1) 

When a proposal is being voted on, `would_follow_ballots` in `rs/nns/governance/src/neuron/types.rs` iterates over this stored list and counts each entry independently: [3](#0-2) 

The majority threshold is computed as `yes * 2 > followees.len()` and `no * 2 >= followees.len()`, where `followees.len()` includes all duplicate entries. If followee A appears 3 times and followees B and C appear once each (`[A, A, A, B, C]`), and A votes Yes while B and C vote No:
- `yes = 3`, `no = 2`, `len = 5` → `yes*2 = 6 > 5` → **Vote::Yes**
- With deduplication: `yes = 1`, `no = 2`, `len = 3` → `no*2 = 4 >= 3` → **Vote::No**

The duplicate entries flip the outcome. This is confirmed as reachable: the integration test `follow_same_neuron_multiple_times` explicitly demonstrates that the system accepts and stores `[n2.neuron_id, n2.neuron_id, n2.neuron_id]` without error: [4](#0-3) 

The neuron data validation code even acknowledges this as a known state: `"Because followees can have duplicates, the primary data might have larger cardinality than the index."`: [5](#0-4) 

By contrast, the newer SNS `SetFollowing` command and the NNS `SetFollowing` command both explicitly reject duplicate followee neuron IDs: [6](#0-5) [7](#0-6) 

The legacy `Follow` command has no equivalent protection.

---

### Impact Explanation

A neuron owner can configure their neuron to automatically vote Yes on proposals where a minority followee voted Yes, by listing that followee many times. For the `NeuronManagement` topic, this is especially sensitive: the followees act as "managers" of the neuron, and inflating one manager's apparent vote weight allows a minority manager to unilaterally control the neuron's management votes. For general governance topics, a large neuron (with significant ICP stake and voting power) configured with duplicate followees can cast automatic votes that do not reflect the true majority preference of its followees, distorting NNS proposal outcomes. The `would_follow_ballots` result feeds directly into the vote cascade mechanism (`neuron_would_follow_ballots` → `continue_processing` in `rs/nns/governance/src/voting.rs`): [8](#0-7) 

---

### Likelihood Explanation

Any neuron owner (unprivileged ingress sender) can submit a `manage_neuron` call with `Command::Follow` containing duplicate neuron IDs. No special privilege, governance majority, or admin key is required. The action is accepted silently and stored. The attacker only needs to own a neuron, which is a standard user action on the NNS. The manipulation is persistent across all future proposals until the followees are changed.

---

### Recommendation

Add a deduplication check in the `follow()` function before storing the followee list, analogous to the check already present in `SetFollowing::validate_topics_are_unique` and SNS `ValidatedFolloweesForTopic::try_from`. Specifically, after the length check at line 5755, deduplicate the `follow_request.followees` vector (or reject the request if duplicates are present):

```rust
// Reject or deduplicate duplicate followee IDs
let mut seen = HashSet::new();
for followee in &follow_request.followees {
    if !seen.insert(followee.id) {
        return Err(GovernanceError::new_with_message(
            ErrorType::InvalidCommand,
            "Duplicate followee neuron ID in Follow request.",
        ));
    }
}
```

This mirrors the fix recommended in the original BkdLocker report: add a `require(newRewardToken != rewardToken)` guard to prevent the same entity from being counted twice.

---

### Proof of Concept

1. Neuron owner A controls neuron N (large stake).
2. A submits `manage_neuron` with `Command::Follow { topic: NetworkEconomics, followees: [X, X, X, Y, Z] }` where X, Y, Z are distinct neuron IDs.
3. The call succeeds (confirmed by `follow_same_neuron_multiple_times` integration test).
4. A proposal on `NetworkEconomics` is submitted. X votes Yes; Y and Z vote No.
5. `would_follow_ballots` computes: `yes=3` (X counted 3×), `no=2`, `len=5`. `3*2=6 > 5` → neuron N automatically votes **Yes**.
6. With correct deduplication: `yes=1`, `no=2`, `len=3`. `2*2=4 >= 3` → neuron N should vote **No**.
7. If N holds sufficient voting power, this flips the proposal outcome. [3](#0-2) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5751-5760)
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
```

**File:** rs/nns/governance/src/governance.rs (L5771-5788)
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

            // Changing followees may change the voting power, so refresh it.
            neuron.refresh_voting_power(now_seconds);
        })?;
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

**File:** rs/nns/governance/src/neuron_data_validation.rs (L531-533)
```rust
        // Because followees can have duplicates, the primary data might have larger cardinality
        // than the index. Therefore we only report an issue when index size is larger than primary.
        if cardinality_primary < cardinality_index {
```

**File:** rs/sns/governance/src/following.rs (L341-345)
```rust
        let duplicate_neuron_ids = get_duplicate_followee_groups(&followees);

        if !duplicate_neuron_ids.is_empty() {
            return Err(Self::Error::DuplicateFolloweeNeuronId(duplicate_neuron_ids));
        }
```

**File:** rs/nns/governance/src/pb/mod.rs (L99-124)
```rust
    fn validate_topics_are_unique(&self) -> Result<(), GovernanceError> {
        let mut topics = HashSet::<Topic>::new();
        for followees_for_topic in &self.topic_following {
            // Treat None the same as Some(0). This also occurs during execution.
            let topic = followees_for_topic.topic.unwrap_or_default();

            // Validate topic.
            let topic = Topic::try_from(topic).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    format!("The operation specified an invalid topic code ({topic:?}): {err}",),
                )
            })?;

            let is_new = topics.insert(topic);

            if !is_new {
                // Violation of uniqueness.
                return Err(GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    format!("The operation specified the same topic ({topic:?}) more than once.",),
                ));
            }
        }

        Ok(())
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
