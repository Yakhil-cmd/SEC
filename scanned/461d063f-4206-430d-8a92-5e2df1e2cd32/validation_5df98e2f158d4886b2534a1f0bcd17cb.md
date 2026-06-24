### Title
Incomplete State Cleanup in `StableNeuronStore::delete()` Leaves Orphaned `maturity_disbursements_map` Entries - (File: `rs/nns/governance/src/storage/neurons.rs`)

---

### Summary

The `StableNeuronStore::delete()` function in the NNS Governance canister cleans up five of six auxiliary stable storage maps when a neuron is removed, but silently omits cleanup of `maturity_disbursements_map`. This is a direct structural analog to the EIP-7579 finding where `onUninstall()` fails to delete from the `walletSessionKeys` mapping. Orphaned entries persist in stable memory, corrupt governance accounting metrics, and can cause the maturity-disbursement finalization timer to encounter unexpected error paths.

---

### Finding Description

`StableNeuronStore` decomposes each neuron across seven `StableBTreeMap`s: [1](#0-0) 

The `update()` method correctly synchronizes all six auxiliary maps, including `maturity_disbursements_map`: [2](#0-1) 

The `delete()` method, however, cleans up only five of the six auxiliary maps and **never touches `maturity_disbursements_map`**: [3](#0-2) 

The missing line is the exact analog of the EIP-7579 `delete walletSessionKeys[msg.sender]` omission:

```rust
// Missing from delete():
update_repeated_field(neuron_id, vec![], &mut self.maturity_disbursements_map);
```

`delete()` is called from `NeuronStore::remove_neuron()`, which is the sole path for neuron removal in the governance canister: [4](#0-3) 

---

### Impact Explanation

**1. Zombie stable-memory entries.** Any `MaturityDisbursement` records keyed by the deleted `NeuronId` remain in `maturity_disbursements_map` indefinitely, consuming stable memory that can never be reclaimed without a manual migration.

**2. Corrupted accounting metric.** `total_maturity_disbursements_in_progress_e8s_equivalent()` sums every value in `maturity_disbursements_map`: [5](#0-4) 

After a neuron with pending disbursements is deleted, this function returns an inflated ICP-e8s figure. Governance dashboards, monitoring alerts, and any on-chain logic that reads this value will observe phantom maturity that does not correspond to any live neuron.

**3. Validation blind spot.** `MaturityDisbursementIndexValidator` only flags the case where the secondary index is *larger* than the primary store, not the reverse: [6](#0-5) 

Orphaned primary-store entries (primary > index) are silently accepted, so the periodic data-validation sweep will not surface the inconsistency.

**4. Timer error path.** `finalize_maturity_disbursement` selects neurons from the store that have non-empty `maturity_disbursements_in_progress`. Because it reads through `neuron_store` (which reads `main`), it will not attempt to process orphaned entries directly. However, the inflated `lens().maturity_disbursements` count can mislead operators and automated tooling into believing disbursements are still pending.

---

### Likelihood Explanation

A neuron can be deleted (via `disburse`, `merge`, or governance-initiated removal) while it still has entries in `maturity_disbursements_map` if:

- A `DisburseMaturity` command was accepted and the resulting `MaturityDisbursement` was appended to the neuron's queue, but the finalization timer has not yet run.
- The neuron's stake is then fully disbursed in the same round or a subsequent round before the timer fires.

This is an edge-case race between the finalization timer and neuron removal, but it is reachable by any neuron holder without any privileged access. The NNS Governance canister is a high-value, always-on production canister, making even low-frequency corruption significant.

---

### Recommendation

Add the missing cleanup call inside `StableNeuronStore::delete()`, immediately after the existing `update_repeated_field` calls:

```rust
pub fn delete(&mut self, neuron_id: NeuronId) -> Result<(), NeuronStoreError> {
    // ... existing main.remove logic ...

    update_repeated_field(neuron_id, vec![], &mut self.hot_keys_map);
    update_repeated_field(neuron_id, vec![], &mut self.recent_ballots_map);
+   update_repeated_field(neuron_id, vec![], &mut self.maturity_disbursements_map);
    self.update_followees(neuron_id, hashmap![]);

    update_singleton_field(neuron_id, None, &mut self.known_neuron_data_map);
    update_singleton_field(neuron_id, None, &mut self.transfer_map);

    Ok(())
}
```

Additionally, extend the existing "no dangling references" test in `neurons_tests.rs` to assert that `maturity_disbursements_map` contains no entries keyed by the deleted `NeuronId` after `delete()` returns.

---

### Proof of Concept

1. Create a neuron with non-zero maturity via the NNS Governance canister.
2. Call `manage_neuron` with `DisburseMaturity`; this appends a `MaturityDisbursement` to the neuron and writes it to `maturity_disbursements_map`.
3. Before the `finalize_maturity_disbursement` timer fires, fully disburse the neuron's stake, triggering `NeuronStore::remove_neuron()` → `StableNeuronStore::delete()`.
4. After deletion, call `stable_neuron_store.lens().maturity_disbursements` — it returns a non-zero count despite no live neurons existing.
5. Call `total_maturity_disbursements_in_progress_e8s_equivalent()` — it returns the phantom ICP-e8s amount of the deleted neuron's pending disbursement.
6. Iterate `maturity_disbursements_map` directly and observe entries whose `NeuronId` key no longer exists in `main`. [3](#0-2) [1](#0-0) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/storage/neurons.rs (L115-131)
```rust
pub(crate) struct StableNeuronStore<Memory>
where
    Memory: ic_stable_structures::Memory,
{
    main: StableBTreeMap<NeuronId, AbridgedNeuron, Memory>,

    // Collections
    hot_keys_map: StableBTreeMap<(NeuronId, /* index */ u64), Principal, Memory>,
    recent_ballots_map: StableBTreeMap<(NeuronId, /* index */ u64), BallotInfo, Memory>,
    followees_map: StableBTreeMap<FolloweesKey, NeuronId, Memory>,
    maturity_disbursements_map:
        StableBTreeMap<(NeuronId, /* index */ u64), MaturityDisbursement, Memory>,

    // Singletons
    known_neuron_data_map: StableBTreeMap<NeuronId, KnownNeuronData, Memory>,
    transfer_map: StableBTreeMap<NeuronId, NeuronStakeTransfer, Memory>,
}
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

**File:** rs/nns/governance/src/storage/neurons.rs (L408-431)
```rust
    /// Removes an existing element.
    ///
    /// Returns Err if not found (and no changes are made, of course).
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

**File:** rs/nns/governance/src/storage/neurons.rs (L575-591)
```rust
    /// Returns the number of entries for some of the storage sections.
    pub fn lens(&self) -> NeuronStorageLens {
        NeuronStorageLens {
            hot_keys: self.hot_keys_map.len(),
            followees: self.followees_map.len(),
            known_neuron_data: self.known_neuron_data_map.len(),
            maturity_disbursements: self.maturity_disbursements_map.len(),
        }
    }

    /// Returns the total amount of maturity disbursements in progress in e8s equivalent.
    pub fn total_maturity_disbursements_in_progress_e8s_equivalent(&self) -> u64 {
        self.maturity_disbursements_map
            .values()
            .map(|maturity_disbursement| maturity_disbursement.amount_e8s)
            .fold(0, |acc, x| acc.saturating_add(x))
    }
```

**File:** rs/nns/governance/src/neuron_store.rs (L403-420)
```rust
    pub fn remove_neuron(&mut self, neuron_id: &NeuronId) {
        let load_neuron_result = self.load_neuron_all_sections(*neuron_id);
        let neuron_to_remove = match load_neuron_result {
            Ok(load_neuron_result) => load_neuron_result,
            Err(error) => {
                println!(
                    "{}WARNING: cannot find neuron {:?} while trying to remove it: {}",
                    LOG_PREFIX, *neuron_id, error
                );
                return;
            }
        };

        let _remove_result = with_stable_neuron_store_mut(|stable_neuron_store| {
            stable_neuron_store.delete(*neuron_id)
        });
        self.remove_neuron_from_indexes(&neuron_to_remove);
    }
```

**File:** rs/nns/governance/src/neuron_data_validation.rs (L626-645)
```rust
    fn validate_cardinalities(_neuron_store: &NeuronStore) -> Option<ValidationIssue> {
        let cardinality_primary = with_stable_neuron_store(|stable_neuron_store| {
            stable_neuron_store.lens().maturity_disbursements
        });
        let cardinality_index =
            with_stable_neuron_indexes(|indexes| indexes.maturity_disbursement().num_entries())
                as u64;
        // Because there can be multiple maturity disbursements for the same neuron and finalization
        // timestamp, the primary data might have larger cardinality than the index. Therefore we
        // only report an issue when index size is larger than primary.
        if cardinality_primary < cardinality_index {
            Some(
                ValidationIssue::MaturityDisbursementIndexCardinalityMismatch {
                    primary: cardinality_primary,
                    index: cardinality_index,
                },
            )
        } else {
            None
        }
```
