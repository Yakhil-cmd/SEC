### Title
Duplicate Followee Entries in NNS Governance `Follow` Command Corrupt Majority-Vote Calculation - (`rs/nns/governance/src/governance.rs`, `rs/nns/governance/src/neuron/types.rs`)

---

### Summary

The NNS governance `follow()` function accepts a `Vec<NeuronId>` of followees for a topic and stores it verbatim without deduplication. The `would_follow_ballots()` function then iterates over this list and counts yes/no votes, counting each duplicate entry as an independent vote. This allows any neuron controller to corrupt the majority-vote calculation that drives cascade voting, making their neuron vote in a direction that does not reflect the true majority of distinct followees.

---

### Finding Description

The `follow()` function in `rs/nns/governance/src/governance.rs` stores the caller-supplied followees list directly into the neuron's `Followees` struct without any deduplication:

```rust
Followees {
    followees: follow_request.followees.clone(),
}
``` [1](#0-0) 

The `would_follow_ballots()` function in `rs/nns/governance/src/neuron/types.rs` then iterates over this raw list and counts yes/no votes per entry:

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
``` [2](#0-1) 

Because `followees.len()` includes duplicates, a duplicate entry inflates both the numerator (the vote count for the duplicated neuron) and the denominator (total followees), but the numerator grows faster, skewing the majority threshold.

This is explicitly confirmed as accepted behavior in two places. The integration test documents it:

> "neurons can follow the same neuron multiple times" [3](#0-2) 

And the neuron data validator explicitly notes:

> "Both followees and principals (controller is a hot key) have duplicates since we do allow it at this time." [4](#0-3) 

The `neuron_would_follow_ballots` wrapper in `NeuronStore` delegates directly to `would_follow_ballots` and is called during cascade vote processing: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

**Concrete example**: Suppose neuron B follows `[A, A, C]` (A duplicated). A votes Yes, C votes No.
- `yes = 2`, `no = 1`, `followees.len() = 3`
- `yes * 2 = 4 > 3` → **Vote::Yes**

Without the duplicate, B follows `[A, C]`:
- `yes = 1`, `no = 1`, `followees.len() = 2`
- `yes * 2 = 2 = 2` → tie → **Vote::No** (tie defaults to No per the `>=` condition)

A neuron controller can flip their neuron's cascade vote from No to Yes (or vice versa) by duplicating a single followee. This corrupts the liquid-democracy mechanism: the neuron appears to be following a set of trusted neurons, but the actual vote outcome is manipulated by the controller. This affects NNS proposal outcomes, including governance, upgrade, and treasury proposals.

---

### Likelihood Explanation

Any neuron controller (unprivileged ingress sender) can call `manage_neuron` with a `Follow` command containing duplicate `NeuronId` entries. No special privilege, key, or threshold is required. The `follow()` function enforces only a length cap (`MAX_FOLLOWEES_PER_TOPIC`) and topic validity, but no deduplication. [7](#0-6) 

The attack is trivially constructable and persistent (the corrupted followees list is stored in stable neuron state).

---

### Recommendation

Deduplicate the followees list before storing it in the `follow()` function. Replace:

```rust
Followees {
    followees: follow_request.followees.clone(),
}
```

with a version that deduplicates while preserving order (using a `HashSet` seen-set), or reject the request with an error if duplicates are detected, consistent with how the newer `SetFollowing` / SNS governance path handles this case. [8](#0-7) 

The SNS governance `ValidatedFolloweesForTopic::try_from` already enforces `DuplicateFolloweeNeuronId` as an error. The NNS `Follow` command should apply the same invariant.

---

### Proof of Concept

1. Neuron controller calls `manage_neuron` with:
   ```
   Follow { topic: <any non-NeuronManagement topic>, followees: [N, N, M] }
   ```
   where N and M are valid neuron IDs.

2. The `follow()` function stores `[N, N, M]` verbatim. [9](#0-8) 

3. When N votes Yes and M votes No on a proposal, `would_follow_ballots` computes `yes=2, no=1, len=3` → returns `Vote::Yes`, even though the true distinct-followee majority is a tie (1 Yes, 1 No) which should resolve to `Vote::No`. [2](#0-1) 

4. The cascade voting machinery propagates this manipulated vote. [6](#0-5)

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

**File:** rs/nns/governance/src/governance.rs (L5771-5781)
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

**File:** rs/nns/governance/src/neuron_data_validation.rs (L783-786)
```rust
    fn test_validator_valid() {
        // Both followees and principals (controller is a hot key) have duplicates since we do allow
        // it at this time.
        let neuron = NeuronBuilder::new(
```

**File:** rs/nns/governance/src/neuron_store.rs (L774-787)
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

**File:** rs/sns/governance/src/following.rs (L339-345)
```rust
        let followees = followees.into_iter().collect();

        let duplicate_neuron_ids = get_duplicate_followee_groups(&followees);

        if !duplicate_neuron_ids.is_empty() {
            return Err(Self::Error::DuplicateFolloweeNeuronId(duplicate_neuron_ids));
        }
```
