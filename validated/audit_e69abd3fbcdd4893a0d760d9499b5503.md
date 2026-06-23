### Title
Duplicate Followees in NNS `Follow` Command Inflate Vote Count in `would_follow_ballots`, Skewing Majority Calculation - (File: `rs/nns/governance/src/neuron/types.rs`)

### Summary

The NNS governance `Follow` command accepts duplicate neuron IDs in the followees list without any deduplication check. The `would_follow_ballots` function iterates over the raw (non-deduplicated) followees list and counts yes/no votes using `followees.len()` as the denominator. A neuron owner can therefore artificially inflate the effective weight of a chosen followee by repeating its ID, causing their neuron to vote Yes (or No) when the true majority of unique followees would produce the opposite or no vote.

### Finding Description

**Entry point — `Follow` command accepts duplicates:**

The `follow` function in NNS governance only checks that the list length does not exceed `MAX_FOLLOWEES_PER_TOPIC`; it performs no deduplication or uniqueness check on the neuron IDs supplied. [1](#0-0) 

The integration test explicitly documents and accepts this behaviour: [2](#0-1) 

The NNS `SetFollowing` command's `validate_intrinsically` likewise only checks topic uniqueness and followee count — it never checks for duplicate neuron IDs within a single topic's followee list: [3](#0-2) 

**Downstream effect — `would_follow_ballots` counts duplicates multiple times:**

When the voting cascade fires, `would_follow_ballots` iterates over the raw `followees` vector (which may contain duplicates) and uses `followees.len()` — the inflated length — as the denominator for the majority test: [4](#0-3) 

Because the same neuron ID can appear multiple times, its ballot is read and counted on each iteration. The majority thresholds `yes * 2 > len` and `no * 2 >= len` are therefore evaluated against an inflated denominator, producing a different (incorrect) result compared to a deduplicated list.

**Concrete example:**

| Followees stored | X votes | Y votes | Z votes | `yes` | `no` | `len` | Result |
|---|---|---|---|---|---|---|---|
| `[X, Y, Z]` (unique) | Yes | No | No | 1 | 2 | 3 | `no*2=4 ≥ 3` → **No** |
| `[X, X, X, Y, Z]` (X triplicated) | Yes | No | No | 3 | 2 | 5 | `yes*2=6 > 5` → **Yes** |

By triplicating X, the neuron votes **Yes** even though two out of three unique followees voted **No**.

### Impact Explanation

Any neuron owner can override the true majority of their followees by duplicating a preferred followee's ID up to `MAX_FOLLOWEES_PER_TOPIC` times. This corrupts the liquid-democracy guarantee that a neuron follows the *majority* of its followees: the neuron instead follows the artificially amplified minority. Because the NNS relies on cascading follow relationships to aggregate voting power across a large number of neurons, systematic use of this technique by neurons with significant staked ICP could shift proposal outcomes away from what the genuine followee majority would produce.

### Likelihood Explanation

The attack requires only a standard `manage_neuron` ingress call with a `Follow` command — no privileged role, no key material, and no coordination with other parties. The integration test suite explicitly confirms the behaviour is accepted today. Any neuron controller or hot-key holder can exploit this immediately.

### Recommendation

Add a duplicate-neuron-ID check inside `follow` (and `SetFollowing`'s `validate_intrinsically`) analogous to the check already present in SNS governance's `ValidatedFolloweesForTopic::try_from`: [5](#0-4) 

Alternatively, deduplicate the followees vector before storing it, so that `would_follow_ballots` always operates on a set of unique IDs.

### Proof of Concept

1. Neuron owner calls `manage_neuron` with:
   ```
   Follow { topic: NetworkEconomics, followees: [X, X, X, Y, Z] }
   ```
   The call succeeds — no error is returned.

2. Followee X votes **Yes**; followees Y and Z vote **No**.

3. `would_follow_ballots` executes:
   - Iterates `[X, X, X, Y, Z]` → `yes = 3`, `no = 2`, `len = 5`
   - `3 * 2 = 6 > 5` → returns `Vote::Yes`

4. The following neuron's ballot is filled in as **Yes** via `cast_vote_and_cascade_follow`, even though the genuine majority (Y and Z) voted **No**. [6](#0-5) [7](#0-6)

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

**File:** rs/nns/governance/src/pb/mod.rs (L92-97)
```rust
    pub fn validate_intrinsically(&self) -> Result<(), GovernanceError> {
        self.validate_topics_are_unique()?;
        self.validate_not_too_many_followees()?;

        Ok(())
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

**File:** rs/sns/governance/src/following.rs (L339-345)
```rust
        let followees = followees.into_iter().collect();

        let duplicate_neuron_ids = get_duplicate_followee_groups(&followees);

        if !duplicate_neuron_ids.is_empty() {
            return Err(Self::Error::DuplicateFolloweeNeuronId(duplicate_neuron_ids));
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
