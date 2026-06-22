### Title
NNS Governance: Duplicate Followee IDs in `Follow` Command Skew Majority Calculation - (`rs/nns/governance/src/neuron/types.rs`)

### Summary

The NNS Governance canister's `Follow` command accepts a `Vec<NeuronId>` for followees with no deduplication. The `would_follow_ballots` function iterates over this raw vector and counts votes, using `followees.len()` as the denominator for the majority threshold. If a neuron ID appears multiple times in the list, its vote is counted multiple times while the denominator is inflated, skewing the majority calculation in a way that is fully attacker-controlled.

### Finding Description

The `Follow` message in the NNS governance proto stores followees as a `repeated` (i.e., `Vec`) field:

```proto
message Follow {
  Topic topic = 1;
  repeated ic_nns_common.pb.v1.NeuronId followees = 2;
}
``` [1](#0-0) 

The `follow` handler in `rs/nns/governance/src/governance.rs` enforces only a length cap (`MAX_FOLLOWEES_PER_TOPIC`) but performs **no deduplication** before storing the list: [2](#0-1) 

The stored list is then used verbatim in `would_follow_ballots`:

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
if yes.saturating_mul(2_usize) > followees.len() { return Vote::Yes; }
if no.saturating_mul(2_usize) >= followees.len() { return Vote::No; }
``` [3](#0-2) 

Because `followees.len()` counts duplicates, an attacker can craft a followee list that artificially inflates the denominator while also inflating the numerator for a chosen neuron, manipulating the majority threshold.

The integration test `follow_same_neuron_multiple_times` explicitly confirms the system **accepts** duplicate followees without error: [4](#0-3) 

### Impact Explanation

An unprivileged neuron owner can call `manage_neuron` with a `Follow` command listing the same followee neuron ID multiple times (up to `MAX_FOLLOWEES_PER_TOPIC` = 15 entries). This distorts the majority vote calculation for that neuron's automatic following:

1. **Artificial majority amplification**: If a neuron owner wants their neuron to always follow neuron X, they can list X fifteen times. When X votes Yes, `yes = 15` and `followees.len() = 15`, so `15*2 > 15` → the follower votes Yes. But if X votes No, `no = 15` and `15*2 >= 15` → the follower votes No. This is equivalent to following a single neuron, but the attacker can also mix duplicates of two neurons to shift the effective majority threshold in their favor.

2. **Threshold manipulation**: With a list like `[A, A, A, A, A, A, A, A, B, B, B, B, B, B, B]` (8 A's, 7 B's), A needs only 8 votes (its own) to reach majority (`8*2=16 > 15`), while B needs 8 votes but only has 7 entries. This lets an attacker bias their neuron's automatic vote toward a preferred followee without the other followee being able to override it, even though the nominal list appears to have two followees.

3. **Governance manipulation**: Since NNS governance uses liquid democracy at scale, neurons with large voting power that are misconfigured this way can have their automatic votes skewed, affecting proposal outcomes.

### Likelihood Explanation

The attack path is fully reachable by any neuron owner via the public `manage_neuron` ingress endpoint. No privileged access is required. The integration test confirms the system accepts duplicate followees. The `MAX_FOLLOWEES_PER_TOPIC` limit (15) is the only constraint, and duplicates count toward it, so an attacker can fill all 15 slots with duplicates of a single neuron. [5](#0-4) 

### Recommendation

Deduplicate the followee list before storing it, or reject requests containing duplicate neuron IDs. The simplest fix is to deduplicate in the `follow` function before the length check:

```rust
let mut seen = BTreeSet::new();
let followees: Vec<_> = follow_request.followees.iter()
    .filter(|id| seen.insert(id.id))
    .cloned()
    .collect();
if followees.len() > MAX_FOLLOWEES_PER_TOPIC { ... }
```

Alternatively, reject the request with an error if duplicates are detected, consistent with how SNS governance handles this case via `get_duplicate_followee_groups`. [6](#0-5) 

### Proof of Concept

1. A neuron owner calls `manage_neuron` with:
   ```
   Follow {
     topic: Topic::NetworkEconomics,
     followees: [NeuronId{id:42}, NeuronId{id:42}, NeuronId{id:42},
                 NeuronId{id:42}, NeuronId{id:42}, NeuronId{id:42},
                 NeuronId{id:42}, NeuronId{id:42}, NeuronId{id:99}]
   }
   ```
2. The list is stored verbatim. `followees.len() = 9`.
3. When neuron 42 votes Yes: `yes = 8`, `8*2=16 > 9` → follower votes Yes.
4. When neuron 99 votes Yes but neuron 42 votes No: `no = 8`, `8*2=16 >= 9` → follower votes No, overriding neuron 99's Yes vote entirely.
5. The attacker has effectively given neuron 42 a 8/9 weight and neuron 99 a 1/9 weight, despite the nominal list appearing to have two followees.

The root cause is in `would_follow_ballots` at `rs/nns/governance/src/neuron/types.rs` lines 484–500, which iterates the raw `Vec` without deduplication, and in the `follow` handler at `rs/nns/governance/src/governance.rs` lines 5751–5760, which stores the list without deduplication. [3](#0-2) [2](#0-1)

### Citations

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L929-934)
```text
  message Follow {
    option (ic_base_types.pb.v1.tui_signed_message) = true;
    // Topic UNSPECIFIED means add following for the 'catch all'.
    Topic topic = 1 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
    repeated ic_nns_common.pb.v1.NeuronId followees = 2 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
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

**File:** rs/sns/governance/src/following.rs (L151-166)
```rust
fn get_duplicate_followee_groups(followees: &BTreeSet<ValidatedFollowee>) -> FolloweeGroups {
    followees
        .iter()
        .sorted_by_key(|followee| followee.neuron_id.clone())
        .group_by(|followee| followee.neuron_id.clone())
        .into_iter()
        .filter_map(|(neuron_id, group)| {
            let followees_with_this_neuron_id = group.into_iter().cloned().collect::<Vec<_>>();

            if followees_with_this_neuron_id.len() > 1 {
                Some((neuron_id, followees_with_this_neuron_id))
            } else {
                None
            }
        })
        .collect()
```
