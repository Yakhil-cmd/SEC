Audit Report

## Title
Duplicate `NeuronPermission` Entries Allow Permanent Permission Retention After Revocation in SNS Neurons for Neurons' Fund Participants — (`rs/sns/governance/src/types.rs`)

## Summary

`NeuronRecipe::construct_permissions` builds the initial `Vec<NeuronPermission>` for SNS neurons created during swap finalization without any deduplication check. When an NF participant's NNS neuron controller principal `P` also appears in `nns_neuron_hotkeys`, `construct_permissions` pushes two separate `NeuronPermission` entries for `P`. The downstream `remove_permissions_for_principal` uses `iter().position()` to find only the first matching entry; after removing it, the second entry survives, permanently granting `P` permissions that governance intended to revoke.

## Finding Description

**Root cause — `construct_permissions` (rs/sns/governance/src/types.rs, lines 2353–2407):**

The function unconditionally pushes one entry for `nns_neuron_controller` and then one entry per hotkey with no membership check:

```rust
permissions.push(NeuronPermission::new(&nns_neuron_controller, ...));
for hotkey in nns_neuron_hotkeys.principals.iter() {
    permissions.push(NeuronPermission::new(hotkey, ...));  // no dedup
}
``` [1](#0-0) 

If `nns_neuron_controller == P` and `P ∈ nns_neuron_hotkeys`, the resulting `Vec<NeuronPermission>` contains two entries with `principal = Some(P)`.

**Validation gap — `NeuronRecipe::validate` (rs/sns/governance/src/types.rs, lines 2273–2288):**

The only check on the constructed permissions is a raw length comparison against `max_number_of_principals_per_neuron`. There is no duplicate-principal check, so a recipe with two entries for the same principal passes validation if the total count is within the limit. [2](#0-1) 

**Neuron stored with duplicates — `claim_swap_neurons` (rs/sns/governance/src/governance.rs, lines 4507–4510):**

After validation passes, the neuron is constructed directly from `construct_permissions_or_panic` and stored:

```rust
let neuron = Neuron {
    permissions: neuron_recipe.construct_permissions_or_panic(neuron_claimer_permissions.clone()),
    ...
};
``` [3](#0-2) 

**Single-entry removal — `remove_permissions_for_principal` (rs/sns/governance/src/neuron.rs, lines 739–786):**

The function uses `iter().position()` which returns only the index of the **first** matching entry. After removing all permission types from that entry and calling `swap_remove`, the second duplicate entry for the same principal remains in the `permissions` Vec: [4](#0-3) 

**Index inconsistency — `remove_neuron_permissions` (rs/sns/governance/src/governance.rs, lines 4700–4713):**

When `AllPermissionTypesRemoved` is returned, the caller removes `P` from `principal_to_neuron_ids_index`. But the neuron's own `permissions` Vec still contains the second entry for `P`, creating a permanent inconsistency: the index says `P` has no access, but the neuron's ACL still grants it. [5](#0-4) 

**Premature capacity block — `add_neuron_permissions` (rs/sns/governance/src/governance.rs, lines 4619–4630):**

The capacity guard compares `neuron.permissions.len()` against `max_number_of_principals_per_neuron`. With an inflated length from the duplicate entry, this limit is hit prematurely, blocking legitimate permission grants to other principals. [6](#0-5) 

**Precondition enabler — `add_hot_key` (rs/nns/governance/src/neuron/types.rs, lines 657–675):**

NNS `add_hot_key` only checks for duplicates within the existing `hot_keys` list, not against the neuron's controller. A user can therefore successfully register their own controller principal as a hotkey: [7](#0-6) 

**No cross-field dedup in hotkey selection — `pick_most_important_hotkeys` (rs/nns/governance/src/neurons_fund.rs, lines 1963–1994):**

This function deduplicates within the hotkeys list but does not filter out principals that equal `nns_neuron_controller`, so the controller-as-hotkey survives into the `NeuronsFund.nns_neuron_hotkeys` field passed to `construct_permissions`. [8](#0-7) 

## Impact Explanation

An NF participant's SNS neuron is created with two `NeuronPermission` entries for the same principal `P`. After a `RemoveNeuronPermissions` governance action removes all permission types from the first entry, the second entry survives. `P` retains the permissions associated with the hotkey entry (e.g., `Vote`, `DisburseMaturity`, `SubmitProposal`) that governance intended to revoke. The `principal_to_neuron_ids_index` is incorrectly updated to reflect full removal, creating a persistent inconsistency between the index and the neuron's actual ACL state. Additionally, the inflated `permissions.len()` prematurely blocks other principals from being added to the neuron.

This matches the allowed impact: **High — Unauthorized access to neurons/governance assets where exploitation requires meaningful per-target work or other constraints.** The attacker retains governance capabilities (voting, maturity disbursement) on an SNS neuron after an authorized revocation, undermining SNS governance integrity.

## Likelihood Explanation

Any NNS neuron holder who has joined the Neurons' Fund can trigger this by calling `manage_neuron → AddHotKey { new_hot_key: <own_controller> }` on their NNS neuron before swap finalization. This is a deliberate, low-effort, unprivileged action requiring no special access. The condition is stable across swap finalization: `pick_most_important_hotkeys` does not filter it out, and `construct_permissions` does not deduplicate it. The bug is deterministically reproducible for any NF participant who performs this one-step setup.

## Recommendation

In `construct_permissions` (`rs/sns/governance/src/types.rs`), collect all principals into a `HashSet` before building the `Vec`, or check for membership before each `push`:

```rust
let mut seen = HashSet::new();
let mut permissions = vec![];
let mut push_if_new = |p: &PrincipalId, perms| {
    if seen.insert(*p) {
        permissions.push(NeuronPermission::new(p, perms));
    }
};
push_if_new(controller, neuron_claimer_permissions.permissions.clone());
push_if_new(&nns_neuron_controller, PERMISSIONS_FOR_NEURONS_FUND_NNS_NEURON_CONTROLLER...);
for hotkey in nns_neuron_hotkeys { push_if_new(hotkey, PERMISSIONS_FOR_NEURONS_FUND_NNS_NEURON_HOTKEY...); }
```

Alternatively, add a duplicate-principal check in `NeuronRecipe::validate` that rejects any recipe whose constructed permissions Vec contains two entries with the same `principal` field. A defense-in-depth fix in `pick_most_important_hotkeys` to also filter out principals matching `nns_neuron_controller` would eliminate the precondition entirely.

## Proof of Concept

1. Alice holds an NNS neuron with controller principal `P` and joins the Neurons' Fund.
2. Alice calls `manage_neuron → Configure → AddHotKey { new_hot_key: P }` on her NNS neuron. This succeeds because `add_hot_key` only checks for duplicates within `hot_keys`, not against the controller.
3. An SNS swap commits. The swap canister calls `claim_swap_neurons` with a `NeuronRecipe` where `nns_neuron_controller = P` and `nns_neuron_hotkeys = [P, ...]`.
4. `construct_permissions` pushes `NeuronPermission{principal: P, types: CONTROLLER_PERMS}` and then `NeuronPermission{principal: P, types: HOTKEY_PERMS}` — two entries for `P`.
5. `NeuronRecipe::validate` passes (total count ≤ `max_number_of_principals_per_neuron`).
6. The neuron is stored with `permissions` containing two entries for `P`.
7. A governance action calls `RemoveNeuronPermissions` for `P` with all permission types. `remove_permissions_for_principal` finds the first entry at position `i`, removes all its types, calls `swap_remove(i)`, and returns `AllPermissionTypesRemoved`.
8. The second entry for `P` (with `HOTKEY_PERMS`) remains in `neuron.permissions`. `P` is removed from `principal_to_neuron_ids_index`, but `P` can still exercise hotkey permissions (e.g., `Vote`, `DisburseMaturity`) on the neuron.

A minimal unit test can be written against `construct_permissions` directly: construct a `NeuronRecipe` with `nns_neuron_controller = P` and `nns_neuron_hotkeys = [P]`, call `construct_permissions`, assert `permissions.len() == 2` (demonstrating the duplicate), then call `remove_permissions_for_principal` for `P` with all permission types, and assert that `permissions` is empty — the assertion will fail, proving the stale entry survives.

### Citations

**File:** rs/sns/governance/src/types.rs (L2273-2288)
```rust
        match self.construct_permissions(NeuronPermissionList::default()) {
            Ok(permissions) => {
                if permissions.len() > max_number_of_principals_per_neuron as usize {
                    defects.push(format!(
                        "Neuron recipe would correspond to a neuron with ({}) permissions ({:?}), exceeding the maximum \
                            number of permissions ({})",
                        permissions.len(),
                        permissions,
                        max_number_of_principals_per_neuron
                    ));
                }
            }
            Err(e) => {
                defects.push(e);
            }
        }
```

**File:** rs/sns/governance/src/types.rs (L2378-2403)
```rust
            permissions.push(NeuronPermission::new(
                &nns_neuron_controller,
                Neuron::PERMISSIONS_FOR_NEURONS_FUND_NNS_NEURON_CONTROLLER
                    .iter()
                    .map(|p| *p as i32)
                    .collect(),
            ));

            for hotkey in neurons_fund_participant
                .nns_neuron_hotkeys
                .as_ref()
                .ok_or(
                    "Expected the nns_neuron_hotkeys to be present for NeuronsFundParticipant"
                        .to_string(),
                )?
                .principals
                .iter()
            {
                permissions.push(NeuronPermission::new(
                    hotkey,
                    Neuron::PERMISSIONS_FOR_NEURONS_FUND_NNS_NEURON_HOTKEY
                        .iter()
                        .map(|p| *p as i32)
                        .collect(),
                ));
            }
```

**File:** rs/sns/governance/src/governance.rs (L4507-4510)
```rust
            let neuron = Neuron {
                id: Some(neuron_id.clone()),
                permissions: neuron_recipe
                    .construct_permissions_or_panic(neuron_claimer_permissions.clone()),
```

**File:** rs/sns/governance/src/governance.rs (L4619-4630)
```rust
        // If the PrincipalId does not already exist in the neuron, make sure it can be added
        if existing_permissions.is_none()
            && neuron.permissions.len() == max_number_of_principals_per_neuron as usize
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Cannot add permission to neuron. Max \
                    number of principals reached {max_number_of_principals_per_neuron}"
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L4700-4713)
```rust
        let principal_id_was_removed = self
            .get_neuron_result_mut(neuron_id)?
            .remove_permissions_for_principal(
                principal_id,
                permissions_to_remove.permissions.clone(),
            )?;

        if principal_id_was_removed == RemovePermissionsStatus::AllPermissionTypesRemoved {
            GovernanceProto::remove_neuron_from_principal_in_principal_to_neuron_ids_index(
                &mut self.principal_to_neuron_ids_index,
                neuron_id,
                &principal_id,
            )
        }
```

**File:** rs/sns/governance/src/neuron.rs (L739-786)
```rust
        let existing_permission_position = self
            .permissions
            .iter()
            .position(|p| p.principal == Some(principal_id))
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::AccessControlList,
                    format!(
                        "PrincipalId {} does not have any permissions in Neuron {}",
                        principal_id,
                        self.id.as_ref().expect("Neuron must have a NeuronId")
                    ),
                )
            })?;

        let existing_permission = self
            .permissions
            .get_mut(existing_permission_position)
            .expect("Expected permission to exist");

        // Initialize a structure to efficiently remove provided permission_types from
        // existing permission_types.
        let mut remaining_permission_types: HashSet<i32> =
            HashSet::from_iter(existing_permission.permission_type.iter().cloned());

        // Initialize a structure to track if permission_types were present in the existing NeuronPermission
        let mut missing_permissions = HashSet::new();
        for permission_type in &permission_types_to_remove {
            let permission_type_is_present = remaining_permission_types.remove(permission_type);
            if !permission_type_is_present {
                missing_permissions.insert(NeuronPermissionType::try_from(*permission_type).ok());
            }
        }

        if !missing_permissions.is_empty() {
            return Err(GovernanceError::new_with_message(
                ErrorType::AccessControlList,
                format!(
                    "PrincipalId {principal_id} was missing permissions {missing_permissions:?} when removing {permission_types_to_remove:?}"
                ),
            ));
        }

        // If there are no remaining permissions after removing the requested permissions, remove
        // the NeuronPermission entry from the neuron.
        if remaining_permission_types.is_empty() {
            self.permissions.swap_remove(existing_permission_position);
            return Ok(RemovePermissionsStatus::AllPermissionTypesRemoved);
```

**File:** rs/nns/governance/src/neuron/types.rs (L657-675)
```rust
    fn add_hot_key(&mut self, new_hot_key: &PrincipalId) -> Result<(), GovernanceError> {
        // Make sure that the same hot key is not added twice.
        for key in &self.hot_keys {
            if *key == *new_hot_key {
                return Err(GovernanceError::new_with_message(
                    ErrorType::HotKey,
                    "Hot key duplicated.",
                ));
            }
        }
        // Allow at most 10 hot keys per neuron.
        if self.hot_keys.len() >= MAX_NUM_HOT_KEYS_PER_NEURON {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached the maximum number of hotkeys.",
            ));
        }
        self.hot_keys.push(*new_hot_key);
        Ok(())
```

**File:** rs/nns/governance/src/neurons_fund.rs (L1963-1994)
```rust
    pub fn pick_most_important_hotkeys(hotkeys: &Vec<PrincipalId>) -> Vec<PrincipalId> {
        // Remove duplicates while preserving the order.
        let mut unique_hotkeys = vec![];
        let mut non_self_auth_hotkeys = vec![];
        let mut observed = HashSet::new();
        for hotkey in hotkeys {
            if !observed.contains(hotkey) {
                observed.insert(*hotkey);
                // Collect hotkeys that are self-authenticating; save non_self_auth_hotkeys for
                // later, in case there is still space for some of them.
                if hotkey.is_self_authenticating() {
                    unique_hotkeys.push(*hotkey);
                } else {
                    non_self_auth_hotkeys.push(*hotkey);
                }
            }
            // Limit how many hotkeys may be collected.
            if unique_hotkeys.len() == MAX_HOTKEYS_FROM_NEURONS_FUND_NEURON {
                break;
            }
        }

        // If there is space in `unique_hotkeys`, fill it up using `non_self_auth_hotkeys`.
        while unique_hotkeys.len() < MAX_HOTKEYS_FROM_NEURONS_FUND_NEURON
            && !non_self_auth_hotkeys.is_empty()
        {
            let non_self_authenticating_hotkey = non_self_auth_hotkeys.remove(0);
            unique_hotkeys.push(non_self_authenticating_hotkey);
        }

        unique_hotkeys
    }
```
