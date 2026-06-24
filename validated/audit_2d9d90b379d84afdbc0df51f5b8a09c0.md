Audit Report

## Title
`set_following` Leaves Stale Entries in `function_followee_index` After Removing Legacy Following — (`rs/sns/governance/src/governance.rs`)

## Summary
In `Governance::set_following`, the legacy cleanup loop calls `neuron.followees.remove(function)` before calling `legacy::remove_neuron_from_function_followee_index_for_function`. Because the helper reads `neuron.followees.get(&function)` to discover which followees to evict and returns early on `None`, the prior removal makes the index update a guaranteed no-op. Stale entries persist in `function_followee_index`, causing the neuron to continue auto-voting alongside its old followees on every subsequent proposal, even though the neuron owner believes the following relationship has been severed.

## Finding Description
The defect is confirmed at two locations in `governance.rs`.

**Location 1 — per-function loop** [1](#0-0) 

`neuron.followees.remove(function)` executes first (line 4113), then `remove_neuron_from_function_followee_index_for_function` is called (line 4115). Inside that helper: [2](#0-1) 

`neuron.followees.get(&function)` returns `None` because the key was already removed, triggering an immediate early return. The `function_followee_index` is never modified.

**Location 2 — catch-all block** [3](#0-2) 

Identical ordering defect: `neuron.followees.remove(&catchall_function)` precedes the index removal call, making it a no-op for the same reason.

The correct pattern is already used for topic-based following just above in the same function — the index is updated *before* the primary data is mutated: [4](#0-3) 

`cast_vote_and_cascade_follow` consumes `function_followee_index` directly for vote propagation: [5](#0-4) 

The index type is `BTreeMap<u64, BTreeMap<String, BTreeSet<NeuronId>>>` (function_id → followee_id → set of follower neuron IDs): [6](#0-5) 

No existing guard compensates for the stale entries. The `follow` (legacy) path correctly reads the index before mutating the neuron, confirming the correct pattern was known: [7](#0-6) 

## Impact Explanation
The `function_followee_index` is the sole data structure used by `cast_vote_and_cascade_follow` to propagate votes from a followee to its registered followers. Because stale entries remain after `set_following`, a neuron that has called `SetFollowing` to remove legacy following will continue to automatically cast votes alongside its old followees on every proposal whose action falls under the affected functions. The neuron owner has no way to detect or correct this: `neuron.followees` is correctly cleared (so reading the neuron shows no following), but the cascade logic uses only the index. A well-positioned followee can permanently retain delegated voting power from any follower that has attempted to opt out via `SetFollowing`, enabling governance manipulation in SNS DAOs. This matches the allowed impact: **High — Significant SNS security impact with concrete user or protocol harm.**

## Likelihood Explanation
`SetFollowing` is the actively promoted migration path for SNS neurons moving from legacy function-based following to topic-based following. The entry path is a standard unprivileged `manage_neuron` ingress call — no special role, key, or privilege is required. The bug fires on every invocation of `set_following` where the neuron has pre-existing legacy followees for the mentioned topics. Any SNS neuron owner who migrates following relationships triggers this silently and irreversibly (without a governance upgrade).

## Recommendation
Swap the order of operations in both affected blocks so the index is updated before the primary map is mutated, mirroring the correct pattern used for topic-based following:

```diff
 for function in native_functions.union(&custom_functions) {
-    neuron.followees.remove(function);
-
-    legacy::remove_neuron_from_function_followee_index_for_function(
+    legacy::remove_neuron_from_function_followee_index_for_function(
         &mut self.function_followee_index,
         neuron,
         *function,
     );
+
+    neuron.followees.remove(function);
 }
```

Apply the same fix to the catch-all block:

```diff
+legacy::remove_neuron_from_function_followee_index_for_function(
+    &mut self.function_followee_index,
+    neuron,
+    catchall_function,
+);
 neuron.followees.remove(&catchall_function);
-
-legacy::remove_neuron_from_function_followee_index_for_function(
-    &mut self.function_followee_index,
-    neuron,
-    catchall_function,
-);
```

## Proof of Concept
1. SNS neuron A has legacy following: `followees = { function_id_X: [neuron_B] }`. The `function_followee_index` contains `function_id_X → neuron_B → {neuron_A}`.
2. Neuron A's owner calls `manage_neuron` → `SetFollowing` with a topic covering `function_id_X` and an empty followees list.
3. Inside `set_following`: `neuron.followees.remove(&function_id_X)` executes — `neuron.followees` is now `{}`. Then `remove_neuron_from_function_followee_index_for_function(index, neuron, function_id_X)` executes — `neuron.followees.get(&function_id_X)` returns `None` → early return. Index unchanged.
4. `function_followee_index[function_id_X][neuron_B]` still contains neuron A's ID.
5. Neuron B votes Yes on proposal P (action type `function_id_X`).
6. `cast_vote_and_cascade_follow` looks up `function_followee_index[function_id_X][neuron_B]` → finds neuron A → automatically casts neuron A's vote as Yes.
7. Neuron A's owner observes their neuron voted Yes despite having removed the following relationship.

A deterministic integration test can be written using `PocketIc` or the existing SNS governance test harness: set up neuron A following neuron B on a legacy function, call `SetFollowing` to remove it, have neuron B vote, and assert that neuron A's ballot remains `Unspecified`.

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

**File:** rs/sns/governance/src/governance.rs (L4010-4027)
```rust
        if let Some(neuron_followees) = neuron.followees.get(&f.function_id) {
            // If this function_id is not represented in the neuron's followees,
            // there is nothing to be removed.
            if let Some(followee_index) = self.function_followee_index.get_mut(&f.function_id) {
                // We need to remove this neuron as a follower
                // for all followees.
                for followee in &neuron_followees.followees {
                    if let Some(all_followers) = followee_index.get_mut(&followee.to_string()) {
                        all_followers.remove(id);
                    }
                    // Note: we don't check that the
                    // function_followee_index actually contains this
                    // neuron's ID as a follower for all the
                    // followees. This could be a warning, but
                    // it is not actionable.
                }
            }
        }
```

**File:** rs/sns/governance/src/governance.rs (L4093-4099)
```rust
        // Second, remove the neuron from the follower index, which needs to be done before
        // replacing the topic followees. Note that mutations begin here, so there should not be any
        // exit points beyond this point.
        remove_neuron_from_follower_index(&mut self.topic_follower_index, neuron);

        // Third, save the new followees.
        neuron.topic_followees.replace(new_topic_followees);
```

**File:** rs/sns/governance/src/governance.rs (L4112-4120)
```rust
            for function in native_functions.union(&custom_functions) {
                neuron.followees.remove(function);

                legacy::remove_neuron_from_function_followee_index_for_function(
                    &mut self.function_followee_index,
                    neuron,
                    *function,
                );
            }
```

**File:** rs/sns/governance/src/governance.rs (L4148-4154)
```rust
            neuron.followees.remove(&catchall_function);

            legacy::remove_neuron_from_function_followee_index_for_function(
                &mut self.function_followee_index,
                neuron,
                catchall_function,
            );
```

**File:** rs/sns/governance/src/follower_index.rs (L147-147)
```rust
    pub(crate) type FollowerIndex = BTreeMap<u64, BTreeMap<String, BTreeSet<NeuronId>>>;
```

**File:** rs/sns/governance/src/follower_index.rs (L207-209)
```rust
        let Some(followees) = neuron.followees.get(&function) else {
            return;
        };
```
