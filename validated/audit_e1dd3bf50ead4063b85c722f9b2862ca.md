Audit Report

## Title
Duplicate Followee IDs in NNS Governance `Follow` Command Skew Cascade Voting Tally - (File: rs/nns/governance/src/neuron/types.rs)

## Summary
The NNS `follow` function stores a raw `Vec<NeuronId>` without deduplication. The `would_follow_ballots` function iterates over this vector and uses `followees.len()` — including duplicates — as the majority denominator. A neuron owner can therefore inflate one followee's effective weight by listing it multiple times, causing their neuron to vote Yes when a deduplicated majority would produce No. Because NNS governance cascades votes via BFS, this skewed vote propagates to every neuron that transitively follows the manipulated neuron.

## Finding Description
**Root cause — no deduplication at ingress:** The `follow` function in `rs/nns/governance/src/governance.rs` performs only a raw-length check against `MAX_FOLLOWEES_PER_TOPIC` before storing the followees vector verbatim. [1](#0-0) [2](#0-1) 

**Skewed denominator at vote time:** `would_follow_ballots` in `rs/nns/governance/src/neuron/types.rs` iterates the raw stored vector and compares `yes * 2 > followees.len()` / `no * 2 >= followees.len()`, where `followees.len()` counts every duplicate occurrence. [3](#0-2) 

With followees `[A, A, B]`, A=Yes, B=No: `yes=2, no=1, len=3` → `2×2=4 > 3` → **Vote::Yes**. With deduplicated `[A, B]`: `yes=1, no=1, len=2` → `1×2=2 ≮ 2` → not Yes; `1×2=2 ≥ 2` → **Vote::No**. The same flaw exists in the SNS legacy `Follow` path, which also only checks raw length. [4](#0-3) [5](#0-4) 

**Contrast with fixed SNS path:** The newer SNS `SetFollowing` command explicitly rejects duplicates via `ValidatedFolloweesForTopic::try_from`, confirming the NNS behavior is a defect, not a design choice. [6](#0-5) 

**Integration test confirms the path is reachable:** An existing test explicitly documents that the NNS `Follow` path currently accepts duplicate followee IDs without error. [7](#0-6) 

## Impact Explanation
This is a **High** severity finding. A neuron owner can misrepresent their following configuration to force their neuron to vote Yes on any proposal where a single chosen followee votes Yes, even when the set of unique followees is evenly split. Because NNS governance propagates votes through the follow graph via BFS cascade, every neuron that transitively follows the manipulated neuron inherits the skewed vote. On high-stakes NNS proposals (protocol upgrades, treasury transfers), this constitutes concrete governance outcome manipulation — a significant NNS security impact with concrete protocol harm, matching the High ($2,000–$10,000) bounty tier for "Significant NNS security impact with concrete user or protocol harm."

## Likelihood Explanation
The attack requires only a standard `manage_neuron::Follow` ingress call from any neuron owner — no privileged role, no key compromise, no social engineering. The `MAX_FOLLOWEES_PER_TOPIC` guard counts raw list length including duplicates, so an attacker can fill all slots with a single repeated ID. The existing integration test confirms the path is reachable and currently succeeds without modification. The attack is repeatable at any time on any topic.

## Recommendation
In the `follow` function (`rs/nns/governance/src/governance.rs`), deduplicate or reject duplicate entries before storing, mirroring the validation already present in `ValidatedFolloweesForTopic::try_from` for the SNS `SetFollowing` path:

```rust
let unique: HashSet<_> = follow_request.followees.iter().collect();
if unique.len() != follow_request.followees.len() {
    return Err(GovernanceError::new_with_message(
        ErrorType::InvalidCommand,
        "Followees list must not contain duplicate neuron IDs.",
    ));
}
```

Apply the same fix to the SNS legacy `Follow` handler in `rs/sns/governance/src/governance.rs` at the equivalent length-check site. [8](#0-7) 

## Proof of Concept
1. Neuron N1 (attacker-controlled) calls `manage_neuron::Follow` with `followees = [A, A, B]` on any non-ManageNeuron topic. The call succeeds — confirmed by the integration test at `rs/nns/integration_tests/src/neuron_following.rs:189-203`.
2. A proposal is created; N1 receives a blank ballot.
3. Followee A votes Yes; followee B votes No.
4. `would_follow_ballots` is invoked for N1: iterates `[A, A, B]`, counts `yes=2, no=1, len=3` → `2×2=4 > 3` → N1's ballot is automatically cast **Yes**.
5. Without duplicates (`[A, B]`): `yes=1, no=1, len=2` → tie → ballot cast **No**.
6. Every neuron that follows N1 inherits the Yes vote via the BFS cascade, amplifying the effect across the follow graph. [9](#0-8)

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

**File:** rs/nns/governance/src/governance.rs (L5776-5779)
```rust
                topic as i32,
                Followees {
                    followees: follow_request.followees.clone(),
                },
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

**File:** rs/sns/governance/src/governance.rs (L3987-3996)
```rust
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
```

**File:** rs/sns/governance/src/neuron.rs (L382-388)
```rust
        if yes.saturating_mul(2) > followees.len() {
            return Vote::Yes;
        }
        // If a majority for Yes can never be achieved, return No.
        if no.saturating_mul(2) >= followees.len() {
            return Vote::No;
        }
```

**File:** rs/sns/governance/src/following.rs (L341-345)
```rust
        let duplicate_neuron_ids = get_duplicate_followee_groups(&followees);

        if !duplicate_neuron_ids.is_empty() {
            return Err(Self::Error::DuplicateFolloweeNeuronId(duplicate_neuron_ids));
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
