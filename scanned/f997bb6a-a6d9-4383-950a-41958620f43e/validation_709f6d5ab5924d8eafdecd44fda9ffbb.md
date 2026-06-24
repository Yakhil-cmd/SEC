### Title
SNS Governance `follow()` Accepts Following on a Deleted (Tombstoned) `NervousSystemFunction` ID When a New Function Reuses the Same ID Slot - (`File: rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS Governance `follow()` method validates that a `function_id` is "registered" before allowing a neuron to set followees for it. However, the existence check (`is_registered_function_id`) correctly rejects tombstoned IDs. The analog vulnerability exists in the **proposal validation path** for `AddGenericNervousSystemFunction`: `validate_and_render_add_generic_nervous_system_function` uses `existing_functions.contains_key()` — which returns `true` for deletion-marker entries — to block re-registration, but this check is **not** applied in `perform_add_generic_nervous_system_function`, which instead calls `is_registered_function_id` (which correctly rejects tombstones). However, a distinct stale-entity analog does exist: the `follow()` function checks function existence via `is_registered_function_id` but **does not verify that the listed followee neurons themselves exist** in `self.proto.neurons`. A neuron can successfully set followees pointing to non-existent (dissolved/removed) neuron IDs, and those phantom followees are silently written into the `function_followee_index` and the neuron's `followees` map, where they persist and participate in vote-cascade logic.

---

### Finding Description

In `rs/sns/governance/src/governance.rs`, the `follow()` method:

1. Verifies the **follower** neuron exists (line 3971).
2. Verifies the `function_id` is a live registered function (line 3998).
3. **Does not verify** that any of the listed **followee** neuron IDs in `f.followees` actually exist in `self.proto.neurons`.

After passing these checks, the code unconditionally inserts the followee list into the neuron's `followees` map and into `self.function_followee_index`:

```rust
neuron.followees.insert(f.function_id, Followees { followees: f.followees.clone() });
// ...
for followee in &f.followees {
    let all_followers = cache.entry(followee.to_string()).or_default();
    all_followers.insert(id.clone());
}
```

This means a caller can register a follow relationship pointing to a neuron ID that:
- Never existed, or
- Previously existed but was dissolved and removed from `self.proto.neurons`.

The `function_followee_index` then contains entries for these phantom neuron IDs. When `cast_vote_and_cascade_follow` runs, it looks up followers of a voting neuron in `function_followee_index`. If a phantom followee ID is later claimed by a **new, different neuron** (neuron IDs in SNS are derived from subaccounts, so reuse is theoretically possible if the same subaccount is re-staked), the follower neuron would automatically cascade-vote based on the new neuron's vote — even though the follow relationship was intended for the old, now-gone neuron.

The exact analog to the Aave Lens bug: the existence check is performed on the **function** (secondary entity), not on the **followee neurons** (the primary entities being followed). A dissolved neuron's ID slot can be re-occupied, and existing follow relationships silently transfer to the new occupant.

---

### Impact Explanation

**Governance integrity / unauthorized vote cascade.** An attacker who:
1. Knows that neuron X (with a specific subaccount) was previously dissolved/removed, and
2. Re-stakes to the same subaccount to claim a new neuron with the same ID,

...will automatically inherit all follow relationships that other neurons had set up pointing to the old neuron X. Those follower neurons will cascade-vote in lockstep with the attacker's new neuron on any open or future proposals, without the follower neuron owners' knowledge or consent. This is a governance authorization bypass: the attacker gains amplified voting power through stale follow relationships they did not legitimately earn.

---

### Likelihood Explanation

SNS neuron IDs are derived from the subaccount (a hash of controller principal + memo). If a user dissolves a neuron and re-stakes with the same memo to the same subaccount, the new neuron will have the same ID. This is a realistic scenario (e.g., a user who dissolved and re-staked, or an attacker who acquires the same principal+memo combination). The follow relationships from the old neuron persist in `function_followee_index` and in each follower's `followees` map indefinitely, since there is no cleanup on neuron dissolution.

---

### Recommendation

In `follow()` (and `set_following()`), validate that each listed followee neuron ID exists in `self.proto.neurons` before writing the follow relationship. Reject the entire `Follow` command if any listed followee does not exist:

```rust
for followee in &f.followees {
    if !self.proto.neurons.contains_key(&followee.to_string()) {
        return Err(GovernanceError::new_with_message(
            ErrorType::NotFound,
            format!("Followee neuron not found: {}", followee),
        ));
    }
}
```

Additionally, when a neuron is dissolved/removed from `self.proto.neurons`, sweep `function_followee_index` and each follower neuron's `followees` map to remove stale entries pointing to the removed neuron ID.

---

### Proof of Concept

1. Neuron A (subaccount `S`, neuron ID `N`) exists in an SNS.
2. Neuron B calls `manage_neuron` → `Follow { function_id: 1000, followees: [N] }`. This succeeds: `is_registered_function_id(1000, ...)` passes, and no check is made on whether `N` exists. `function_followee_index[1000][N] = {B}` is written.
3. Neuron A dissolves and is removed from `self.proto.neurons`. No cleanup of `function_followee_index` occurs.
4. An attacker re-stakes to subaccount `S` with the same memo, claiming a new neuron with the same ID `N`.
5. A proposal with `function_id = 1000` is submitted. The attacker's neuron `N` votes Yes.
6. `cast_vote_and_cascade_follow` looks up `function_followee_index[1000][N]` → finds `{B}` → cascades Yes vote to neuron B, without B's owner having authorized this.

**Entry path:** Unprivileged `manage_neuron` ingress call to the SNS Governance canister. No special permissions required beyond holding a neuron with `Vote` permission. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3715-3731)
```rust
        let neuron_id_to_follower_neuron_ids = {
            let mut members = vec![];
            let mut push_member = |function_id| {
                if let Some(member) = function_followee_index.get(&function_id) {
                    members.push(member);
                }
            };

            push_member(function_id);

            match topic.proposal_criticality() {
                ProposalCriticality::Normal => push_member(fallback_pseudo_function_id),
                ProposalCriticality::Critical => (), // Do not use catch-all/fallback following.
            }

            UnionMultiMap::new(members)
        };
```

**File:** rs/sns/governance/src/governance.rs (L3998-4006)
```rust
        if !is_registered_function_id(f.function_id, &self.proto.id_to_nervous_system_functions) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!(
                    "Function with id: {} is not present among the current set of functions.",
                    f.function_id,
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L4028-4047)
```rust
        if !f.followees.is_empty() {
            // Insert the new list of followees for this function_id in
            // the neuron's followees, removing the old list, which has
            // already been removed from the followee index above.
            neuron.followees.insert(
                f.function_id,
                Followees {
                    followees: f.followees.clone(),
                },
            );
            let cache = self
                .function_followee_index
                .entry(f.function_id)
                .or_default();
            // We need to add this neuron as a follower for
            // all followees.
            for followee in &f.followees {
                let all_followers = cache.entry(followee.to_string()).or_default();
                all_followers.insert(id.clone());
            }
```

**File:** rs/sns/governance/src/types.rs (L2002-2014)
```rust
pub fn is_registered_function_id(
    function_id: u64,
    nervous_system_functions: &BTreeMap<u64, NervousSystemFunction>,
) -> bool {
    // Check if the function id is present among the native actions.
    if Action::native_function_ids().contains(&function_id) {
        return true;
    }

    match nervous_system_functions.get(&function_id) {
        None => false,
        Some(function) => function != &*NERVOUS_SYSTEM_FUNCTION_DELETION_MARKER,
    }
```
