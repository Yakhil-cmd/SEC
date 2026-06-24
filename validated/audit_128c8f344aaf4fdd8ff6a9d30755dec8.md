Audit Report

## Title
NNS Governance `Follow` Command Stores Duplicate Followee IDs, Inflating Vote Weight in `would_follow_ballots` - (File: rs/nns/governance/src/neuron/types.rs)

## Summary

The NNS `follow()` function stores a raw `Vec<NeuronId>` without deduplication. The `would_follow_ballots` function iterates over this raw vector and counts votes per list entry, using `followees.len()` (including duplicates) as the majority threshold denominator. Any neuron owner can list the same followee N times, giving that followee N× the vote weight of any other followee, violating the stated "majority of followees" liquid democracy semantics.

## Finding Description

`follow()` in `rs/nns/governance/src/governance.rs` performs only four checks before storing followees: caller authorization, topic validity, followee existence/visibility, and `MAX_FOLLOWEES_PER_TOPIC` count. No deduplication is performed. [1](#0-0) 

The submitted `followees` vector is stored verbatim via `modify_followees` with multiplicity preserved. [2](#0-1) 

`would_follow_ballots` in `rs/nns/governance/src/neuron/types.rs` then iterates over this raw vector. Each occurrence of a neuron ID increments `yes` or `no` independently, and the majority threshold is computed against `followees.len()` — the raw length including duplicates. [3](#0-2) 

The `SetFollowing` path's `validate_intrinsically()` in `rs/nns/governance/src/pb/mod.rs` also omits a duplicate-followee check, validating only topic uniqueness and followee count. [4](#0-3) 

By contrast, the SNS governance path in `rs/sns/governance/src/following.rs` explicitly rejects duplicate followee IDs via `get_duplicate_followee_groups`, demonstrating that DFINITY recognizes this as a correctness requirement. [5](#0-4) 

The integration test `follow_same_neuron_multiple_times` in `rs/nns/integration_tests/src/neuron_following.rs` explicitly confirms the canister accepts duplicate followees without error, with the comment "neurons can follow the same neuron multiple times." [6](#0-5) 

**Exploit flow:** Neuron A follows `[B, B, B, C, D]`. B votes Yes; C and D vote No. `yes = 3`, `no = 2`, `followees.len() = 5`. `3×2 > 5` → `Vote::Yes`. With unique followees `[B, C, D]`, the same votes yield `1×2 > 3` false, `2×2 >= 3` true → `Vote::No`. The automatic vote is reversed.

## Impact Explanation

This is a significant NNS governance security impact. The NNS liquid democracy mechanism is documented to implement "majority of followees" with equal weight per followee. The bug allows any neuron owner to unilaterally violate this invariant, causing their neuron to cast automatic votes that contradict the true majority of unique followees. Neurons with large voting power using this configuration can affect NNS proposal outcomes. NNS governance is explicitly in scope, and governance voting integrity is a concrete protocol harm matching the "High" impact tier: *Significant NNS security impact with concrete user or protocol harm.*

## Likelihood Explanation

No special privilege is required beyond owning a neuron, which any ICP holder can create. The `Follow` command carries no fee. The operation is a standard `manage_neuron` ingress message. The integration test confirms the canister accepts it without error. The attack is fully self-service, cheap, and repeatable.

## Recommendation

In `follow()` (`rs/nns/governance/src/governance.rs`), deduplicate the `followees` list before storing it, or reject requests containing duplicate `NeuronId` values — mirroring the check already present in `ValidatedFolloweesForTopic::try_from` in `rs/sns/governance/src/following.rs`. Apply the same fix to `SetFollowing`'s `validate_intrinsically()` in `rs/nns/governance/src/pb/mod.rs` by adding a step analogous to `validate_topics_are_unique` that checks for duplicate followee IDs within each topic entry.

## Proof of Concept

1. Call `manage_neuron` with `Follow { topic: NetworkEconomics, followees: [B, B, B, C, D] }` from the neuron owner's principal.
2. Confirm the call succeeds (no error returned).
3. Trigger a proposal vote where B votes Yes and C, D vote No.
4. Observe neuron A automatically votes Yes (`yes=3`, `no=2`, `3×2 > 5`).
5. Reset followees to `[B, C, D]` and repeat: neuron A automatically votes No (`yes=1`, `no=2`, `2×2 >= 3`).
6. The existing integration test `follow_same_neuron_multiple_times` in `rs/nns/integration_tests/src/neuron_following.rs` already validates step 1–2; extend it with a proposal vote to confirm the vote-direction reversal.

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

**File:** rs/nns/governance/src/pb/mod.rs (L92-97)
```rust
    pub fn validate_intrinsically(&self) -> Result<(), GovernanceError> {
        self.validate_topics_are_unique()?;
        self.validate_not_too_many_followees()?;

        Ok(())
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
