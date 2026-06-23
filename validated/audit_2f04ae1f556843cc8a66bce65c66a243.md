The code evidence is conclusive. Let me present the findings.

---

### Title
`StableNeuronStore::delete` Omits `maturity_disbursements_map` Cleanup, Leaving Orphaned Entries in Stable Storage — (`rs/nns/governance/src/storage/neurons.rs`)

### Summary

`StableNeuronStore::delete` clears five of the six auxiliary stable maps but silently skips `maturity_disbursements_map`. Any neuron deleted while it has active maturity disbursements leaves orphaned `(NeuronId, index) → MaturityDisbursement` entries that persist across canister upgrades and permanently inflate `total_maturity_disbursements_in_progress_e8s_equivalent()`.

### Finding Description

The `delete` implementation at lines 411–431 clears every auxiliary map **except** `maturity_disbursements_map`: [1](#0-0) 

```
update_repeated_field(neuron_id, vec![], &mut self.hot_keys_map);      // ✓ cleared
update_repeated_field(neuron_id, vec![], &mut self.recent_ballots_map); // ✓ cleared
self.update_followees(neuron_id, hashmap![]);                           // ✓ cleared
update_singleton_field(neuron_id, None, &mut self.known_neuron_data_map); // ✓ cleared
update_singleton_field(neuron_id, None, &mut self.transfer_map);          // ✓ cleared
// maturity_disbursements_map ← NEVER TOUCHED
```

The missing line is:
```rust
update_repeated_field(neuron_id, vec![], &mut self.maturity_disbursements_map);
```

`create` and `update` both correctly call `update_repeated_field` for `maturity_disbursements_map`: [2](#0-1) [3](#0-2) 

`total_maturity_disbursements_in_progress_e8s_equivalent()` iterates **all** values in the map with no existence check against `main`: [4](#0-3) 

The existing zombie-reference test (lines 348–405) checks `hot_keys_map`, `recent_ballots_map`, `followees_map`, `known_neuron_data_map`, and `transfer_map`, but **never checks `maturity_disbursements_map`**, so the gap is untested: [5](#0-4) 

### Impact Explanation

- Orphaned `(NeuronId, index) → MaturityDisbursement` entries accumulate in `maturity_disbursements_map` (a `StableBTreeMap`, so they survive upgrades).
- `total_maturity_disbursements_in_progress_e8s_equivalent()` permanently over-reports the total maturity in flight. If this value is used in any economic or governance accounting (e.g., supply tracking, reward calculations), the inflation is persistent and unbounded.
- Storage bloat in stable memory grows proportionally to the number of deleted neurons that had active disbursements.

### Likelihood Explanation

A normal neuron lifecycle can produce this state: a user initiates maturity disbursement, then dissolves and fully disburses the neuron's stake before the maturity disbursement completes. The governance canister calls `delete()` on the neuron, leaving the maturity disbursement entries orphaned. No privileged access is required — this is a standard user flow.

### Recommendation

Add the missing cleanup line inside `delete()`:

```rust
update_repeated_field(neuron_id, vec![], &mut self.maturity_disbursements_map);
```

Also extend the zombie-reference test to assert `maturity_disbursements_map` contains no entries for the deleted neuron ID.

### Proof of Concept

State-machine test:
1. Create a neuron with one or more `maturity_disbursements_in_progress` entries.
2. Call `store.delete(neuron_id)`.
3. Assert `store.maturity_disbursements_map` contains zero entries for that `neuron_id`.
4. Assert `store.total_maturity_disbursements_in_progress_e8s_equivalent() == 0`.

Both assertions will **fail** against the current code, confirming the orphaned entries.

### Citations

**File:** rs/nns/governance/src/storage/neurons.rs (L222-226)
```rust
        update_repeated_field(
            neuron_id,
            maturity_disbursements_in_progress,
            &mut self.maturity_disbursements_map,
        );
```

**File:** rs/nns/governance/src/storage/neurons.rs (L386-392)
```rust
        if maturity_disbursements_in_progress != old_neuron.maturity_disbursements_in_progress {
            update_repeated_field(
                neuron_id,
                maturity_disbursements_in_progress,
                &mut self.maturity_disbursements_map,
            );
        }
```

**File:** rs/nns/governance/src/storage/neurons.rs (L411-431)
```rust
    pub fn delete(&mut self, neuron_id: NeuronId) -> Result<(), NeuronStoreError> {
        let deleted_neuron = self.main.remove(&neuron_id);

        match deleted_neuron {
            Some(_deleted_neuron) => (),
            None => {
                return Err(NeuronStoreError::not_found(neuron_id));
            }
        }

        // Auxiliary Data
        // --------------
        update_repeated_field(neuron_id, vec![], &mut self.hot_keys_map);
        update_repeated_field(neuron_id, vec![], &mut self.recent_ballots_map);
        self.update_followees(neuron_id, hashmap![]);

        update_singleton_field(neuron_id, None, &mut self.known_neuron_data_map);
        update_singleton_field(neuron_id, None, &mut self.transfer_map);

        Ok(())
    }
```

**File:** rs/nns/governance/src/storage/neurons.rs (L586-591)
```rust
    pub fn total_maturity_disbursements_in_progress_e8s_equivalent(&self) -> u64 {
        self.maturity_disbursements_map
            .values()
            .map(|maturity_disbursement| maturity_disbursement.amount_e8s)
            .fold(0, |acc, x| acc.saturating_add(x))
    }
```

**File:** rs/nns/governance/src/storage/neurons/neurons_tests.rs (L370-405)
```rust
    // No zombies. This requires looking at privates. Normally, we try to avoid
    // this, but APIs normally assume internal consistency, but that is exactly
    // what we're trying to to verify here.
    let original_neuron_id = neuron_1.id();

    assert_no_zombie_references_in(
        "hot_keys",
        &store.hot_keys_map,
        |key, _| key.0,
        original_neuron_id,
    );
    assert_no_zombie_references_in(
        "recent_ballots",
        &store.recent_ballots_map,
        |key, _| key.0,
        original_neuron_id,
    );
    assert_no_zombie_references_in(
        "followees",
        &store.followees_map,
        |_, followee_id| followee_id,
        original_neuron_id,
    );

    assert_no_zombie_references_in(
        "known_neuron_data",
        &store.known_neuron_data_map,
        |key, _| key,
        original_neuron_id,
    );
    assert_no_zombie_references_in(
        "transfer",
        &store.transfer_map,
        |key, _| key,
        original_neuron_id,
    );
```
