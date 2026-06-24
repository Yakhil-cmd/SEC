Audit Report

## Title
Missing Re-validation of Existing Followees Allows Perpetuation of Unauthorized Private-Neuron Follow Relationships — (File: rs/nns/governance/src/governance.rs)

## Summary
The `modify_followees` function unconditionally skips the private-neuron follow restriction check for any followee already present in a neuron's current followee list. Because a followee neuron's visibility can change from public to private after a follow relationship is established, a subsequent `Follow` or `SetFollowing` call on the same topic will carry over the now-private followee without re-validation, permanently circumventing the access-control invariant introduced by Proposal 138991.

## Finding Description
In `modify_followees` (line 8365), after the `is_neuron_follow_restrictions_enabled()` guard (line 8380), the function builds a set of the neuron's current followees for the topic and then iterates over the new followee list. For each followee already in `old_followees`, it unconditionally skips the visibility check:

```rust
// rs/nns/governance/src/governance.rs, lines 8408–8418
let old_followees = topic_to_followees
    .get(&topic)
    .map(|f| f.followees.iter().collect::<HashSet<&NeuronId>>())
    .unwrap_or_default();

for followee in &new_followees.followees {
    if old_followees.contains(followee) {
        continue;  // no visibility check
    }
    // visibility check only runs for genuinely new followees
``` [1](#0-0) 

The visibility check that would block following a private neuron (lines 8430–8432) is therefore never reached for any followee that was already in the list: [2](#0-1) 

Both entry points into `modify_followees` are affected. The `follow` function calls it directly: [3](#0-2) 

The `set_following` path also calls it in a loop over all updated topics: [4](#0-3) 

The exploit flow is:
1. Neuron A follows Neuron B while B is `Visibility::Public`. This passes the check at line 8430.
2. Neuron B's controller calls `SetVisibility { visibility: Private }`. B is now private.
3. Neuron A's controller calls `Follow { topic: Governance, followees: [B, C] }` (e.g., to add a new public neuron C).
4. In `modify_followees`, `old_followees = {B}`. For B: `old_followees.contains(B)` → `continue`. For C: validated normally. Result: A follows both B (private, unauthorized) and C (public, authorized).
5. A standalone `Follow { topic: Governance, followees: [B] }` without C would be rejected because B is private and `principal_A` is not B's controller or hotkey.

The comment at line 8415–8417 ("grandfathered in, or it was already validated when it was created") is the stated rationale, but it does not account for post-creation visibility changes.

## Impact Explanation
This is a governance privacy and authorization bypass. Proposal 138991 introduced the private-neuron follow restriction specifically to prevent unauthorized neurons from mirroring a private neuron's votes, which reveals the private neuron's voting decisions. The bypass allows an unprivileged neuron controller to permanently retain a follow relationship with a private neuron that they could not establish today, causing the private neuron's governance votes to be disclosed through the follower's automatically cast votes. This matches the allowed impact: **Significant NNS security impact with concrete user or protocol harm** (Medium–High). The private neuron's controller explicitly opted into privacy; the bypass defeats that opt-in for any follower who established the relationship before the neuron became private.

## Likelihood Explanation
The preconditions are realistic and already widely satisfied on mainnet: Proposal 138991 was recently introduced, meaning a large number of pre-existing follow relationships exist with neurons that have since become (or may become) private. Step 3 — calling `Follow` or `SetFollowing` on the same topic — is a routine, unprivileged user action requiring no special skill or cost. The attacker does not need to know the followee is private; they simply include it in their updated followee list. The attack is repeatable indefinitely.

## Recommendation
Remove the early-`continue` for existing followees when `is_neuron_follow_restrictions_enabled()` is true. All followees in the submitted list — including previously established ones — should be re-validated against the followee neuron's current visibility and hotkey state at the time of the `Follow` or `SetFollowing` call. If grandfathering of pre-Proposal-138991 relationships is a deliberate policy choice, it should be scoped to a one-time migration rather than applied on every subsequent update, and should not apply when the user explicitly re-submits a followee in a new command.

## Proof of Concept
A deterministic unit test in `rs/nns/governance/src/governance.rs` or its test module:

1. Create Neuron A (controller `principal_A`) and Neuron B (controller `principal_B`, `visibility = Public`).
2. Call `follow(A, principal_A, Follow { topic: Governance, followees: [B] })` — succeeds.
3. Set Neuron B's visibility to `Private` directly in the neuron store.
4. Call `follow(A, principal_A, Follow { topic: Governance, followees: [B, C] })` where C is a new public neuron — assert this **succeeds** (demonstrating the bypass).
5. Call `follow(A, principal_A, Follow { topic: Governance, followees: [B] })` (B alone, no C) — assert this also **succeeds** due to the same grandfathering logic, confirming the relationship persists.
6. Verify that a fresh neuron D calling `follow(D, principal_D, Follow { topic: Governance, followees: [B] })` is **rejected** with `PreconditionFailed`, confirming B is private and the restriction works for new relationships. [5](#0-4)

### Citations

**File:** rs/nns/governance/src/governance.rs (L3143-3149)
```rust
            topic_to_followees = modify_followees(
                &self.neuron_store,
                &neuron,
                &topic_to_followees,
                topic,
                Followees { followees },
            )?;
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

**File:** rs/nns/governance/src/governance.rs (L8408-8419)
```rust
    let old_followees = topic_to_followees
        .get(&topic)
        .map(|f| f.followees.iter().collect::<HashSet<&NeuronId>>())
        .unwrap_or_default();

    for followee in &new_followees.followees {
        if old_followees.contains(followee) {
            // An already existing follow relationship is either
            // grandfathered in, or it was already validated when it was created.
            // Hence, we don't need to validate it again.
            continue;
        }
```

**File:** rs/nns/governance/src/governance.rs (L8430-8432)
```rust
            let allowed_to_follow = followee_visibility == Visibility::Public
                || followee_controller == controller
                || followee_hot_keys.contains(&controller);
```
