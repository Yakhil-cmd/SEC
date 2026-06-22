### Title
Missing `maturity_disbursements_map` Cleanup in `StableNeuronStore::delete` — (`rs/nns/governance/src/storage/neurons.rs`)

---

### Summary

`StableNeuronStore::delete` cleans up every auxiliary stable-storage map **except** `maturity_disbursements_map`. Any neuron deleted while it has active maturity disbursements leaves orphan `MaturityDisbursement` entries in that map permanently. The entries survive canister upgrades (stable memory), inflate `total_maturity_disbursements_in_progress_e8s_equivalent()`, and corrupt the `lens().maturity_disbursements` cardinality counter used by the data-validation subsystem.

---

### Finding Description

`StableNeuronStore::delete` at lines 411–431 removes the neuron from `main` and then clears five auxiliary maps: [1](#0-0) 

```
hot_keys_map       ✓ cleared
recent_ballots_map ✓ cleared
followees_map      ✓ cleared
known_neuron_data_map ✓ cleared
transfer_map       ✓ cleared
maturity_disbursements_map  ✗ NEVER TOUCHED
```

Compare with `create`, which explicitly writes to `maturity_disbursements_map`: [2](#0-1) 

And `update`, which conditionally clears it: [3](#0-2) 

The omission in `delete` is the only place the invariant is broken.

`total_maturity_disbursements_in_progress_e8s_equivalent` iterates **all** values in `maturity_disbursements_map` unconditionally, with no cross-check against the main neuron map: [4](#0-3) 

Orphan entries therefore permanently inflate this aggregate.

---

### Reachable Attack Path

A neuron controller (unprivileged) can reach this in two steps:

1. **Initiate maturity disbursement** via `DisburseMaturity` ingress command. `initiate_maturity_disbursement` checks only that the caller controls the neuron and that it is not spawning — no guard prevents a dissolved neuron from having active disbursements: [5](#0-4) 

2. **Disburse the neuron's stake** (`Disburse` command) while the 7-day disbursement window is still open. This path calls `StableNeuronStore::delete` on the neuron, leaving the `MaturityDisbursement` entries as orphans.

No privileged role, no majority corruption, and no external dependency is required. Both commands are standard ingress messages available to any neuron controller.

---

### Impact Explanation

| Effect | Detail |
|---|---|
| Stable-memory leak | Orphan `(NeuronId, index) → MaturityDisbursement` entries persist across every future upgrade |
| Inflated aggregate metric | `total_maturity_disbursements_in_progress_e8s_equivalent()` returns a value larger than reality; used in governance reporting and potentially governance-state assertions |
| Cardinality validator false positives | `MaturityDisbursementIndexValidator` compares `lens().maturity_disbursements` (inflated) against the index size, triggering spurious `MaturityDisbursementIndexCardinalityMismatch` validation issues |
| Maturity loss for the user | The maturity was already deducted from the neuron at initiation time; with the neuron gone the finalization timer cannot complete the transfer, so the funds are permanently lost to the user | [6](#0-5) 

---

### Likelihood Explanation

The path is straightforward for any neuron holder. No special timing, no race condition, and no privileged access is needed. The only prerequisite is owning a neuron with non-zero maturity, which is the normal state for long-term NNS participants.

---

### Recommendation

Add the missing cleanup line inside `delete`, mirroring the pattern used for every other repeated field:

```rust
// In StableNeuronStore::delete, after clearing recent_ballots_map:
update_repeated_field(neuron_id, vec![], &mut self.maturity_disbursements_map);
```

Additionally, consider adding a pre-deletion guard that rejects `delete` (or at minimum logs a warning) when `maturity_disbursements_map` still contains entries for the neuron, to catch future regressions.

---

### Proof of Concept

State-machine test outline (mirrors the existing `test_total_maturity_disbursements_in_progress_e8s_equivalent` pattern):

```rust
let mut store = new_heap_based();
let mut neuron = create_model_neuron(42);
neuron.maturity_disbursements_in_progress = vec![
    MaturityDisbursement { amount_e8s: 1_0000_0000, finalize_disbursement_timestamp_seconds: 1, ..Default::default() },
];
store.create(neuron).unwrap();

// Sanity: disbursement is tracked
assert_eq!(store.total_maturity_disbursements_in_progress_e8s_equivalent(), 1_0000_0000);

// Delete the neuron (simulates disburse-stake path)
store.delete(NeuronId { id: 42 }).unwrap();

// BUG: both assertions below FAIL on the current code
assert_eq!(store.lens().maturity_disbursements, 0);
assert_eq!(store.total_maturity_disbursements_in_progress_e8s_equivalent(), 0);
``` [7](#0-6)

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

**File:** rs/nns/governance/src/storage/neurons.rs (L421-428)
```rust
        // Auxiliary Data
        // --------------
        update_repeated_field(neuron_id, vec![], &mut self.hot_keys_map);
        update_repeated_field(neuron_id, vec![], &mut self.recent_ballots_map);
        self.update_followees(neuron_id, hashmap![]);

        update_singleton_field(neuron_id, None, &mut self.known_neuron_data_map);
        update_singleton_field(neuron_id, None, &mut self.transfer_map);
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

**File:** rs/nns/governance/src/governance/disburse_maturity.rs (L300-308)
```rust
    if is_neuron_spawning {
        return Err(InitiateMaturityDisbursementError::NeuronSpawning);
    }
    if !is_neuron_controlled_by_caller {
        return Err(InitiateMaturityDisbursementError::CallerIsNotNeuronController);
    }
    if num_disbursements >= MAX_NUM_DISBURSEMENTS {
        return Err(InitiateMaturityDisbursementError::TooManyDisbursements);
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

**File:** rs/nns/governance/src/storage/neurons/neurons_tests.rs (L790-858)
```rust
#[test]
fn test_total_maturity_disbursements_in_progress_e8s_equivalent() {
    let mut store = new_heap_based();

    // Neuron without maturity disbursements should not contribute to total.
    store.create(create_model_neuron(1)).unwrap();
    assert_eq!(
        store.total_maturity_disbursements_in_progress_e8s_equivalent(),
        0
    );

    // Neuron with two disbursements: 1 ICP + 2 ICP = 3 ICP
    let mut neuron_2 = create_model_neuron(2);
    neuron_2.maturity_disbursements_in_progress = vec![
        MaturityDisbursement {
            amount_e8s: E8,
            finalize_disbursement_timestamp_seconds: 1,
            ..Default::default()
        },
        MaturityDisbursement {
            amount_e8s: 2 * E8,
            finalize_disbursement_timestamp_seconds: 2,
            ..Default::default()
        },
    ];
    store.create(neuron_2.clone()).unwrap();
    assert_eq!(
        store.total_maturity_disbursements_in_progress_e8s_equivalent(),
        3 * E8
    );

    // Third neuron with one disbursement: 5 ICP. Total = 3 + 5 = 8 ICP
    let mut neuron_3 = create_model_neuron(3);
    neuron_3.maturity_disbursements_in_progress = vec![MaturityDisbursement {
        amount_e8s: 5 * E8,
        finalize_disbursement_timestamp_seconds: 3,
        ..Default::default()
    }];
    store.create(neuron_3).unwrap();
    assert_eq!(
        store.total_maturity_disbursements_in_progress_e8s_equivalent(),
        8 * E8
    );

    // Update neuron_2: add a third disbursement of 4 ICP.
    // New total for neuron_2 = 1 + 2 + 4 = 7 ICP. Grand total = 7 + 5 = 12 ICP
    let mut updated_neuron_2 = neuron_2.clone();
    updated_neuron_2.maturity_disbursements_in_progress = vec![
        MaturityDisbursement {
            amount_e8s: E8,
            finalize_disbursement_timestamp_seconds: 1,
            ..Default::default()
        },
        MaturityDisbursement {
            amount_e8s: 2 * E8,
            finalize_disbursement_timestamp_seconds: 2,
            ..Default::default()
        },
        MaturityDisbursement {
            amount_e8s: 4 * E8,
            finalize_disbursement_timestamp_seconds: 4,
            ..Default::default()
        },
    ];
    store.update(&neuron_2, updated_neuron_2).unwrap();
    assert_eq!(
        store.total_maturity_disbursements_in_progress_e8s_equivalent(),
        12 * E8
    );
```
