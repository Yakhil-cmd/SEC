### Title
NNS Governance `Follow` Command Accepts Duplicate Followees, Skewing Majority-Vote Calculation — (`rs/nns/governance/src/governance.rs`, `rs/nns/governance/src/neuron/types.rs`)

---

### Summary

The NNS Governance `follow()` handler stores the caller-supplied `followees` `Vec<NeuronId>` verbatim without deduplication. The downstream `would_follow_ballots()` function iterates the raw `Vec` and counts each entry independently, so a duplicate entry for the same neuron ID is counted multiple times. Any neuron controller can exploit this to give one followee disproportionate weight in the majority calculation, effectively bypassing the intended "strict majority of distinct followees" semantics.

---

### Finding Description

**Root cause — no deduplication in `follow()`:**

The `follow()` function in `rs/nns/governance/src/governance.rs` validates only the list length against `MAX_FOLLOWEES_PER_TOPIC` and caller authorization. It then passes the raw `follow_request.followees` clone directly into `modify_followees()`, which inserts it into the neuron's `followees` map without any uniqueness check. [1](#0-0) 

`modify_followees()` itself performs no deduplication either — it simply calls `updated_followees.insert(topic, new_followees)`: [2](#0-1) 

The `Followees` proto type is a plain `Vec`: [3](#0-2) 

**Impact site — `would_follow_ballots()` counts every Vec entry:**

When the vote cascade evaluates whether a follower neuron should be swayed, it calls `would_follow_ballots()`, which iterates the raw `Vec` and increments `yes`/`no` for every entry, including duplicates. The threshold check divides by `followees.len()` — the total entry count, not the unique-neuron count: [4](#0-3) 

**Codebase acknowledgement of the issue:**

The codebase itself notes that duplicates exist and are tolerated: [5](#0-4) [6](#0-5) 

An integration test explicitly demonstrates that a neuron can follow the same neuron three times without error: [7](#0-6) 

---

### Impact Explanation

**Concrete manipulation scenario:**

Suppose `MAX_FOLLOWEES_PER_TOPIC = 15`. A neuron controller submits:

```
followees = [B, B, B, B, B, B, B, B, B, B, B, B, B, C, D]
            (13 copies of B, 1 C, 1 D)
```

The `would_follow_ballots` calculation becomes:

| B votes | C votes | D votes | yes | no | Result |
|---------|---------|---------|-----|----|--------|
| Yes     | No      | No      | 13  | 2  | **Yes** (26 > 15) |
| No      | Yes     | Yes     | 2   | 13 | **No** (26 ≥ 15) |
| —       | Yes     | No      | 1   | 1  | Unspecified |

Neuron A effectively always follows B regardless of C and D, defeating the "majority of distinct followees" design intent. With only `[B, C, D]` (no duplicates), a tie between C and D when B abstains would yield `Unspecified`, and B's single vote would not dominate.

**Governance impact:** Any neuron controller — including controllers of neurons with large staked ICP — can silently concentrate following weight on a single neuron while appearing to follow multiple neurons. This allows a large neuron to be permanently wired to follow one specific neuron's vote on any topic, amplifying that neuron's effective governance influence beyond what the protocol intends. [8](#0-7) 

---

### Likelihood Explanation

- Reachable by any neuron controller via the standard `manage_neuron` → `Follow` ingress call.
- No privileged access, no key compromise, no social engineering required.
- The `Follow` message is a `repeated NeuronId` protobuf field; any client can trivially repeat the same ID.
- The integration test `follow_same_neuron_multiple_times` confirms the canister accepts and persists the duplicates without error. [9](#0-8) 

---

### Recommendation

1. **Deduplicate in `follow()`**: Before calling `modify_followees`, deduplicate `follow_request.followees` (or reject the request if duplicates are present), analogous to how SNS governance's `SetFollowing` path rejects duplicate neuron IDs via `ValidatedFolloweesForTopic::try_from`. [10](#0-9) 

2. **Enforce uniqueness in `modify_followees()`**: Add a check that the incoming `Followees` vec contains no repeated `NeuronId` values before inserting.

3. **Update `MAX_FOLLOWEES_PER_TOPIC` enforcement**: The current length check operates on the raw (possibly duplicate-inflated) list; after deduplication the effective limit should be applied to unique IDs only.

---

### Proof of Concept

```
// Attacker controls neuron A (large staked ICP).
// Attacker wants neuron A to always follow neuron B (e.g., a known-neuron they control).
// They submit via manage_neuron:

ManageNeuron {
  neuron_id_or_subaccount: NeuronId { id: A },
  command: Follow {
    topic: Topic::Governance,
    followees: [B, B, B, B, B, B, B, B, B, B, B, B, B, C, D],
    //          ^^^ 13 copies of B ^^^                   ^ 1 each of C, D
  }
}

// Result stored in neuron A's followees Vec (length 15, accepted without error).
// When B votes Yes on any Governance proposal:
//   yes = 13, no = 0, followees.len() = 15
//   13 * 2 = 26 > 15  →  neuron A votes Yes automatically
// When B votes No:
//   no = 13, 13 * 2 = 26 >= 15  →  neuron A votes No automatically
// C and D's votes are irrelevant; B's vote always determines A's vote.
``` [11](#0-10) [12](#0-11)

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

**File:** rs/nns/governance/src/governance.rs (L8365-8383)
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
```

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L105-108)
```rust
pub struct Followees {
    #[prost(message, repeated, tag = "1")]
    pub followees: ::prost::alloc::vec::Vec<::ic_nns_common::pb::v1::NeuronId>,
}
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

**File:** rs/nns/governance/src/neuron_data_validation.rs (L531-532)
```rust
        // Because followees can have duplicates, the primary data might have larger cardinality
        // than the index. Therefore we only report an issue when index size is larger than primary.
```

**File:** rs/nns/governance/src/storage/neurons/neurons_tests.rs (L40-46)
```rust
                // Not sorted and has duplicates, to make sure we preserve order and
                // multiplicity.
                NeuronId { id: 211 },
                NeuronId { id: 212 },
                NeuronId { id: 210 },
                NeuronId { id: 210 },
            ],
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

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L929-934)
```text
  message Follow {
    option (ic_base_types.pb.v1.tui_signed_message) = true;
    // Topic UNSPECIFIED means add following for the 'catch all'.
    Topic topic = 1 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
    repeated ic_nns_common.pb.v1.NeuronId followees = 2 [(ic_base_types.pb.v1.tui_signed_display_q2_2021) = true];
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
