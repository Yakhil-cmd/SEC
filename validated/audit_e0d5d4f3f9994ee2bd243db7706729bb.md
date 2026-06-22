### Title
Duplicate Followee Neuron IDs in NNS Governance `would_follow_ballots` Allow Manipulation of Following-Based Vote Cascade - (File: rs/nns/governance/src/neuron/types.rs)

### Summary

The NNS governance `would_follow_ballots` function iterates over a raw `Vec<NeuronId>` followees list without deduplication. Because the `Follow` command also does not reject duplicate neuron IDs before storing them, an unprivileged neuron holder can craft a followees list containing the same neuron ID repeated multiple times. This causes a single followee's vote to be counted multiple times in the majority calculation, allowing the attacker to make their neuron cascade-vote in a direction that does not reflect the true majority of their followees.

### Finding Description

The `would_follow_ballots` function in `rs/nns/governance/src/neuron/types.rs` computes how a neuron would vote based on its followees:

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

`followees` is a `&Vec<NeuronId>` extracted directly from the stored proto field. There is no deduplication step before the loop. Each occurrence of a neuron ID in the list is counted independently, and `followees.len()` (the denominator) also includes duplicates.

The `follow()` function in `rs/nns/governance/src/governance.rs` that stores followees performs only a length check (`followees.len() > MAX_FOLLOWEES_PER_TOPIC`) and a visibility/authorization check per followee, but no uniqueness check:

```rust
if follow_request.followees.len() > MAX_FOLLOWEES_PER_TOPIC {
    return Err(...);
}
// ...
neuron.followees.insert(topic, Followees { followees: follow_request.followees.clone() });
```

The `modify_followees` helper and the `SetFollowing` path (`validate_intrinsically`) similarly do not check for duplicate neuron IDs within a topic's followees list. The codebase itself acknowledges this state is reachable: the `neuron_data_validation.rs` comment at line 531 explicitly states "Because followees can have duplicates, the primary data might have larger cardinality than the index."

**Concrete manipulation example:**

Suppose Neuron A sets its followees for a topic to `[B, B, B, B, B, B, B, B, C]` (8 copies of B, 1 copy of C, total = 9 ≤ `MAX_FOLLOWEES_PER_TOPIC = 15`). B votes Yes, C votes No.

- `yes = 8`, `no = 1`, `followees.len() = 9`
- `8 * 2 > 9` → `16 > 9` → **Vote::Yes**

Without duplicates (`[B, C]`):
- `yes = 1`, `no = 1`, `followees.len() = 2`
- `1 * 2 > 2` → False; `1 * 2 >= 2` → True → **Vote::No**

The attacker flips the cascade outcome from No to Yes by duplicating a followee they control.

### Impact Explanation

An attacker who controls a neuron can submit a `Follow` command with a crafted followees list containing duplicate neuron IDs. When a followee they also control votes on a proposal, the cascade (`cast_vote_and_cascade_follow`) will call `would_follow_ballots`, which counts the duplicate votes multiple times. This allows the attacker's neuron to cast a cascade vote that does not reflect the true majority of its followees — effectively amplifying the influence of a single controlled followee neuron to override the votes of other legitimate followees. In NNS governance, this can affect the adoption or rejection of proposals that govern the Internet Computer protocol itself, including parameter changes, canister upgrades, and treasury operations.

### Likelihood Explanation

The attack requires only an unprivileged neuron holder with voting permission (`NeuronPermissionType::Vote` or controller/hotkey status). The `Follow` command is a standard, publicly accessible `manage_neuron` ingress call. No privileged access, key compromise, or subnet majority is needed. The attacker must also control at least one followee neuron that votes on the target proposal. This is a realistic scenario for any neuron holder who follows their own neuron(s).

### Recommendation

1. **Short term**: In `modify_followees` (and the `follow()` function), deduplicate the incoming followees list before storing it, or reject requests containing duplicate neuron IDs with an `InvalidCommand` error. This mirrors the fix already applied in the SNS governance `SetFollowing` path (`rs/sns/governance/src/following.rs`, `DuplicateFolloweeNeuronId` error) and the NNS `SetFollowing` path (`validate_topics_are_unique`).

2. **Long term**: In `would_follow_ballots`, iterate over a deduplicated set of followee IDs rather than the raw `Vec`, as a defense-in-depth measure against any existing stored state with duplicates.

### Proof of Concept

**Entry path**: Any neuron holder calls `manage_neuron` on the NNS governance canister with a `Follow` command.

**Steps**:
1. Attacker controls Neuron A (follower) and Neuron B (followee).
2. Attacker calls `manage_neuron` → `Follow { topic: <target_topic>, followees: [B, B, B, B, B, B, B, B, C] }` for Neuron A, where C is a legitimate independent followee.
3. The `follow()` function at `rs/nns/governance/src/governance.rs:5718` accepts this list (length 9 ≤ 15, B is public or attacker-controlled) and stores it verbatim via `modify_followees`.
4. A proposal of `<target_topic>` is submitted. Neuron B votes Yes; Neuron C votes No.
5. `cast_vote_and_cascade_follow` triggers `would_follow_ballots` for Neuron A.
6. `would_follow_ballots` at `rs/nns/governance/src/neuron/types.rs:486` iterates the raw Vec: B's Yes vote is counted 8 times, C's No vote once. `yes=8, no=1, len=9` → `16 > 9` → Neuron A cascade-votes Yes.
7. Without the duplicate manipulation, the true majority (1 Yes vs 1 No) would have resulted in Neuron A voting No (or Unspecified). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rs/nns/governance/src/governance.rs (L8365-8384)
```rust
fn modify_followees(
    neuron_store: &NeuronStore,
    neuron: &Neuron,
    topic_to_followees: &HashMap<i32, Followees>,
    topic: i32,
    new_followees: Followees,
) -> Result<HashMap<i32, Followees>, GovernanceError> {
    let controller = neuron.controller();
    let mut updated_followees = topic_to_followees.clone();
    if new_followees.followees.is_empty() {
        // If the new followees list is empty, remove the entry for the topic.
        updated_followees.remove(&topic);
        return Ok(updated_followees);
    }

    if !is_neuron_follow_restrictions_enabled() {
        // If the neuron follow restrictions are not enabled, we can directly update the entry.
        updated_followees.insert(topic, new_followees);
        return Ok(updated_followees);
    }
```

**File:** rs/nns/governance/src/neuron_data_validation.rs (L531-532)
```rust
        // Because followees can have duplicates, the primary data might have larger cardinality
        // than the index. Therefore we only report an issue when index size is larger than primary.
```

**File:** rs/nns/governance/src/pb/mod.rs (L92-97)
```rust
    pub fn validate_intrinsically(&self) -> Result<(), GovernanceError> {
        self.validate_topics_are_unique()?;
        self.validate_not_too_many_followees()?;

        Ok(())
    }
```
