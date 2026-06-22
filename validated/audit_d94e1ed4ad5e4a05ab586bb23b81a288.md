### Title
Duplicate Followees in NNS `Follow` Command Corrupt Majority Vote Calculation - (`rs/nns/governance/src/neuron/types.rs`)

---

### Summary

The NNS governance `Follow` command accepts a `Vec<NeuronId>` of followees and stores them without deduplication. The `would_follow_ballots` function then iterates over this list verbatim, counting each entry's vote — including duplicates — toward the majority threshold. This allows any neuron owner to artificially inflate the vote weight of a chosen followee, corrupting the majority calculation that governs automatic vote cascading.

---

### Finding Description

The NNS `follow` handler in `rs/nns/governance/src/governance.rs` validates only that the followee list does not exceed `MAX_FOLLOWEES_PER_TOPIC` in length. It performs no uniqueness check before storing the list: [1](#0-0) 

The followees are stored as a raw `Vec<NeuronId>` inside the `Followees` proto. When a proposal is voted on, `would_follow_ballots` in `rs/nns/governance/src/neuron/types.rs` iterates over this vector and counts yes/no votes per entry: [2](#0-1) 

Because the denominator `followees.len()` and the vote counters `yes`/`no` both include duplicates, a neuron owner who submits `[A, A, B]` as followees gives neuron A two votes in the majority calculation instead of one. The majority threshold `yes * 2 > followees.len()` is then evaluated against an inflated total that does not reflect the number of unique followees.

This is explicitly confirmed as accepted behavior by an integration test: [3](#0-2) 

The comment reads: *"neurons can follow the same neuron multiple times"* — and the call succeeds without error.

The NNS storage layer also explicitly preserves duplicates: [4](#0-3) 

By contrast, the SNS governance's newer `SetFollowing` command explicitly rejects duplicate followee neuron IDs: [5](#0-4) 

The NNS legacy `Follow` command has no equivalent guard.

---

### Impact Explanation

A neuron owner submitting `Follow` with `[N2, N2, N3]` causes the following majority calculation when N2 votes Yes and N3 votes No:

- `yes = 2` (N2 counted twice), `no = 1`, `followees.len() = 3`
- `2 * 2 > 3` → **Yes** (majority achieved)

Without the duplicate, `[N2, N3]` with the same votes yields:

- `yes = 1`, `no = 1`, `followees.len() = 2`
- `1 * 2 >= 2` → **No** (tie defaults to No)

The neuron owner has flipped their neuron's automatic vote from No to Yes by submitting a duplicate followee. This corrupts the liquid democracy mechanism: the following neuron votes in a way that does not reflect the true majority of distinct followees. At scale, this can affect governance outcomes if many neurons use this technique to bias their automatic votes toward a preferred followee.

The impact is a **governance accounting bug** — the majority vote calculation is corrupted by attacker-controlled input, causing incorrect automatic vote cascading in NNS proposals.

---

### Likelihood Explanation

The entry path is fully unprivileged: any NNS neuron owner can call `manage_neuron` with a `Follow` command via ingress. No special role, key, or majority is required. The integration test confirms the call succeeds. The manipulation is subtle and not visible in the neuron's displayed following configuration without inspecting raw followee counts.

---

### Recommendation

Add a uniqueness check in the NNS `follow` handler before storing the followee list, analogous to the check already present in SNS `SetFollowing`:

```rust
// In rs/nns/governance/src/governance.rs, inside `follow()`
let unique_followees: HashSet<_> = follow_request.followees.iter().collect();
if unique_followees.len() != follow_request.followees.len() {
    return Err(GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "Followee list contains duplicate neuron IDs.",
    ));
}
```

Alternatively, deduplicate the list before storing it. The `would_follow_ballots` function should also be hardened to deduplicate its input before counting, as a defense-in-depth measure. [6](#0-5) 

---

### Proof of Concept

1. Neuron owner calls `manage_neuron` with `Follow { topic: T, followees: [N2, N2, N3] }`. This succeeds (confirmed by integration test at line 196–202).
2. A proposal of topic T is submitted. N2 votes Yes, N3 votes No.
3. `would_follow_ballots` is called for the following neuron. It iterates `[N2, N2, N3]`, counting `yes=2, no=1, total=3`.
4. `2 * 2 > 3` → the following neuron automatically votes **Yes**, even though the unique followee split is 1 Yes vs 1 No (which should default to No).
5. Without the duplicate, the same scenario with `[N2, N3]` yields `yes=1, no=1, total=2` → `1*2 >= 2` → **No**.

The neuron owner has unilaterally changed their neuron's automatic vote outcome by exploiting the missing duplicate check. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5718-5780)
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

**File:** rs/sns/governance/src/following.rs (L339-345)
```rust
        let followees = followees.into_iter().collect();

        let duplicate_neuron_ids = get_duplicate_followee_groups(&followees);

        if !duplicate_neuron_ids.is_empty() {
            return Err(Self::Error::DuplicateFolloweeNeuronId(duplicate_neuron_ids));
        }
```
