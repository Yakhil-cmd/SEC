### Title
Duplicate Followee Neuron IDs in NNS `Follow` Command Bypass Majority Threshold - (`rs/nns/governance/src/neuron/types.rs`)

### Summary

The NNS governance canister's legacy `Follow` command (via `manage_neuron`) accepts a `Vec<NeuronId>` of followees without deduplicating them. The `would_follow_ballots` function iterates over this raw list and counts votes, using `followees.len()` as the denominator for the majority threshold. An unprivileged neuron owner can submit a `Follow` request listing the same followee neuron ID multiple times, artificially inflating `followees.len()` while the actual distinct vote count remains small, thereby manipulating the majority threshold used to determine whether the follower neuron casts an automatic vote.

### Finding Description

The `Follow` command in NNS governance stores a `Vec<NeuronId>` (a `repeated` protobuf field) directly into the neuron's `followees` map without any deduplication check. The `would_follow_ballots` function in `rs/nns/governance/src/neuron/types.rs` then iterates over this raw list:

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
```

`followees.len()` is the total count of entries in the raw list, including duplicates. If a neuron owner sets followees as `[A, A, A, B]` (neuron A repeated 3 times, neuron B once), then `followees.len() == 4`. If A votes Yes, `yes == 3` (counted 3 times), and `3 * 2 > 4` → the follower votes Yes. Conversely, if A votes No, `no == 3`, and `3 * 2 >= 4` → the follower votes No. This means a single followee (A) can unilaterally control the follower's vote by being listed multiple times, even when the intent of the majority rule is that a strict majority of *distinct* followees must agree.

The `follow` function in `rs/nns/governance/src/governance.rs` only checks that `follow_request.followees.len() > MAX_FOLLOWEES_PER_TOPIC` (a count-based limit), but does **not** check for duplicate neuron IDs within the list. The integration test `follow_same_neuron_multiple_times` in `rs/nns/integration_tests/src/neuron_following.rs` explicitly confirms this is accepted behavior for the legacy `Follow` path.

By contrast, the newer `SetFollowing` command (NNS) and the SNS `SetFollowing` path both validate uniqueness: `SetFollowing::validate_intrinsically` in `rs/nns/governance/src/pb/mod.rs` checks topic uniqueness but not followee-ID uniqueness within a topic, while SNS's `ValidatedFolloweesForTopic::try_from` in `rs/sns/governance/src/following.rs` does enforce `DuplicateFolloweeNeuronId` rejection. The NNS legacy `Follow` path has no such protection.

### Impact Explanation

An unprivileged neuron owner (any ICP holder with a neuron) can manipulate the automatic vote-cascading behavior of their own neuron. By listing a single followee multiple times, they can:

1. **Lower the effective majority threshold**: Make a single followee's vote sufficient to trigger automatic following, even when the nominal followee list appears to have multiple members.
2. **Prevent automatic voting**: By padding the list with duplicates of a non-voting or abstaining neuron, they can ensure `yes * 2 <= followees.len()` and `no * 2 < followees.len()` always hold, permanently suppressing automatic vote propagation for their neuron on a topic.

The impact is governance integrity: the majority-following mechanism, which is a core part of NNS liquid democracy, can be gamed by any neuron owner to produce non-intuitive automatic voting behavior. This affects the correctness of vote cascading across the NNS.

### Likelihood Explanation

The attack is trivially reachable: any neuron owner can call `manage_neuron` with a `Follow` command containing duplicate neuron IDs. No special privileges, keys, or coordination are required. The integration test `follow_same_neuron_multiple_times` confirms the system accepts this input without error. The attacker controls only their own neuron's following configuration, so the impact is scoped to their neuron's automatic voting behavior.

### Recommendation

In the `follow` function in `rs/nns/governance/src/governance.rs`, after the length check, add a deduplication check:

```rust
let unique_followees: HashSet<u64> = follow_request.followees.iter().map(|f| f.id).collect();
if unique_followees.len() != follow_request.followees.len() {
    return Err(GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "Followee list contains duplicate neuron IDs.",
    ));
}
```

Alternatively, deduplicate the list before storing it, consistent with how the SNS `ValidatedFolloweesForTopic` uses a `BTreeSet` to eliminate duplicates. The `would_follow_ballots` function should also be hardened to use a `HashSet` when counting votes, so that even if duplicates exist in stored state (from before the fix), they do not affect vote counting.

### Proof of Concept

1. Neuron owner controls neuron N. They want N to automatically follow neuron A (and only A) on topic T, but want to prevent any single followee from triggering a "No" vote.

2. Call `manage_neuron` with:
   ```
   Follow { topic: T, followees: [A, A, A, B] }
   ```
   where B is a neuron that never votes. This is accepted (confirmed by `follow_same_neuron_multiple_times` test).

3. Now `followees.len() == 4`. For A's Yes vote to cascade: `yes = 3` (A counted 3×), `3 * 2 = 6 > 4` → N votes Yes. For A's No vote: `no = 3`, `3 * 2 = 6 >= 4` → N votes No. B never votes, so it contributes 0 to either count.

4. Effectively, A alone controls N's vote despite the list nominally having two distinct followees. The majority threshold is bypassed because `followees.len()` counts duplicates.

5. Alternatively, set `followees: [B, B, B, B, A]` where B never votes. Now `followees.len() == 5`. If A votes Yes: `yes = 1`, `1 * 2 = 2 <= 5` → N does **not** vote Yes. If A votes No: `no = 1`, `1 * 2 = 2 < 5` → N does **not** vote No. N's automatic voting is permanently suppressed on topic T, even though A is a followee.

---

**Key file references:**

- `would_follow_ballots` (no dedup, uses raw `followees.len()`): [1](#0-0) 
- `follow` function (only length check, no duplicate check): [2](#0-1) 
- Integration test confirming duplicates are accepted: [3](#0-2) 
- SNS correctly rejects duplicate followee IDs: [4](#0-3) 
- NNS `SetFollowing` validates topic uniqueness but not followee-ID uniqueness within a topic: [5](#0-4)

### Citations

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

**File:** rs/sns/governance/src/following.rs (L341-345)
```rust
        let duplicate_neuron_ids = get_duplicate_followee_groups(&followees);

        if !duplicate_neuron_ids.is_empty() {
            return Err(Self::Error::DuplicateFolloweeNeuronId(duplicate_neuron_ids));
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
