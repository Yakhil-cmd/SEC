### Title
Unbounded `neuron_ids` Input in `list_neurons` Materializes All Pages Before Returning One, Causing Instruction/Memory Exhaustion - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS governance `list_neurons` endpoint accepts a caller-supplied `neuron_ids: Vec<u64>` with no enforced size cap. Before returning a single page, the implementation eagerly materializes **all** pages into a `Vec<Vec<NeuronId>>`. An unprivileged ingress sender can supply the maximum-sized vector allowed by the 2 MB ingress limit (~262 000 entries) and force the canister to perform O(N log N) BTreeSet construction plus O(N) full-chunk materialization per call. The `neuron_subaccounts: Option<Vec<NeuronSubaccount>>` field compounds this: each entry triggers an individual stable-memory lookup before the same chunking path runs.

---

### Finding Description

In `rs/nns/governance/src/governance.rs`, `list_neurons` processes the caller-supplied `neuron_ids` field as follows:

```rust
// No size cap is applied to neuron_ids before this point.
let mut requested_neuron_ids: BTreeSet<NeuronId> =
    neuron_ids.iter().map(|id| NeuronId { id: *id }).collect();   // O(N log N)
requested_neuron_ids.append(&mut implicitly_requested_neuron_ids);
requested_neuron_ids.append(&mut neurons_by_subaccount);

let chunks: Vec<Vec<NeuronId>> = requested_neuron_ids
    .into_iter()
    .chunks(page_size as usize)
    .into_iter()
    .map(|chunk| chunk.collect())
    .collect();                                                     // ALL pages materialised
``` [1](#0-0) 

`page_size` is capped at `MAX_LIST_NEURONS_RESULTS` (500), but there is **no corresponding cap on `neuron_ids`**:

```rust
let page_size = page_size
    .unwrap_or(MAX_LIST_NEURONS_RESULTS as u64)
    .min(MAX_LIST_NEURONS_RESULTS as u64);
``` [2](#0-1) 

The `ListNeurons` type definition confirms `neuron_ids` is an unbounded `Vec<u64>`: [3](#0-2) 

The `neuron_subaccounts` field is equally unbounded (`Option<Vec<NeuronSubaccount>>`), and each entry triggers a stable-memory BTreeMap lookup via `get_neuron_id_for_subaccount` before the same chunking path runs:

```rust
let mut neurons_by_subaccount: BTreeSet<NeuronId> = neuron_subaccounts
    .as_ref()
    .map(|subaccounts| {
        subaccounts
            .iter()
            .flat_map(|neuron_subaccount| {
                Self::bytes_to_subaccount(&neuron_subaccount.subaccount)
                    .ok()
                    .and_then(|subaccount| {
                        self.neuron_store.get_neuron_id_for_subaccount(subaccount)
                    })
            })
            .collect()
    })
    .unwrap_or_default();
``` [4](#0-3) 

The Candid interface exposes both fields without any declared length bound: [5](#0-4) 

---

### Impact Explanation

**Instruction exhaustion / query-call DoS.** With a 2 MB ingress message, a caller can supply ≈262 000 `u64` neuron IDs. The canister must:

1. Insert all of them into a `BTreeSet` — O(N log N) ≈ 4–5 million comparisons.
2. Materialise every chunk into a `Vec<Vec<NeuronId>>` — O(N) allocations — even though only one page (≤500 entries) is ever read.

With `neuron_subaccounts`, each of the ≈62 500 entries (32 bytes each in 2 MB) triggers a stable-memory BTreeMap lookup. Stable-memory reads are significantly more expensive than heap reads; at realistic per-lookup costs the aggregate instruction count can approach or exceed the 5-billion-instruction query limit, causing the call to trap with `CanisterInstructionLimitExceeded`.

Even if the instruction limit is not reached in every scenario, the unnecessary full materialisation of all pages wastes cycles proportional to the total result set rather than the requested page, degrading canister responsiveness under concurrent load.

---

### Likelihood Explanation

`list_neurons` is a publicly callable query (and update) endpoint on the NNS governance canister — no special role, neuron ownership, or privileged key is required. Any boundary-node user or canister can craft a maximum-size ingress message with a large `neuron_ids` or `neuron_subaccounts` vector and submit it repeatedly. The cost to the attacker is only the ingress fee; the cost to the canister scales with N.

---

### Recommendation

1. **Cap `neuron_ids` before processing** — reject or truncate requests where `neuron_ids.len()` exceeds `MAX_LIST_NEURONS_RESULTS` (or a separately defined ingress cap).
2. **Lazy page computation** — replace the eager `.collect()` of all chunks with a lazy skip-and-take so only the requested page is ever materialised:
   ```rust
   let current_page: Vec<NeuronId> = requested_neuron_ids
       .into_iter()
       .skip(page_number as usize * page_size as usize)
       .take(page_size as usize)
       .collect();
   ```
3. **Cap `neuron_subaccounts`** — apply the same size limit to the `neuron_subaccounts` field to bound the number of stable-memory lookups per call.

---

### Proof of Concept

Send the following ingress call to the NNS governance canister from any principal:

```
list_neurons(record {
    neuron_ids       = vec { 1; 2; 3; … /* 262 000 distinct u64 values */ };
    include_neurons_readable_by_caller = false;
    page_number      = opt 0;
    page_size        = opt 500;
    neuron_subaccounts = opt vec {};
    include_empty_neurons_readable_by_caller = opt false;
    include_public_neurons_in_full_neurons   = opt false;
})
```

The canister will insert all 262 000 IDs into a `BTreeSet`, then materialise ≈524 `Vec<NeuronId>` chunks, and finally return only the first 500-entry page. Repeating this call concurrently amplifies the per-round instruction budget consumed by the governance canister, degrading or blocking legitimate governance queries.

For the `neuron_subaccounts` variant, replace `neuron_ids` with an empty vec and supply ≈62 500 `NeuronSubaccount` entries (each 32 bytes), triggering 62 500 stable-memory lookups before the same chunking path runs.

### Citations

**File:** rs/nns/governance/src/governance.rs (L1632-1634)
```rust
        let page_size = page_size
            .unwrap_or(MAX_LIST_NEURONS_RESULTS as u64)
            .min(MAX_LIST_NEURONS_RESULTS as u64);
```

**File:** rs/nns/governance/src/governance.rs (L1664-1678)
```rust
        let mut neurons_by_subaccount: BTreeSet<NeuronId> = neuron_subaccounts
            .as_ref()
            .map(|subaccounts| {
                subaccounts
                    .iter()
                    .flat_map(|neuron_subaccount| {
                        Self::bytes_to_subaccount(&neuron_subaccount.subaccount)
                            .ok()
                            .and_then(|subaccount| {
                                self.neuron_store.get_neuron_id_for_subaccount(subaccount)
                            })
                    })
                    .collect()
            })
            .unwrap_or_default();
```

**File:** rs/nns/governance/src/governance.rs (L1681-1695)
```rust
        let mut requested_neuron_ids: BTreeSet<NeuronId> =
            neuron_ids.iter().map(|id| NeuronId { id: *id }).collect();
        requested_neuron_ids.append(&mut implicitly_requested_neuron_ids);
        requested_neuron_ids.append(&mut neurons_by_subaccount);

        // These will be assembled into the final result.
        let mut neuron_infos = hashmap![];
        let mut full_neurons = vec![];

        let chunks: Vec<Vec<NeuronId>> = requested_neuron_ids
            .into_iter()
            .chunks(page_size as usize)
            .into_iter()
            .map(|chunk| chunk.collect())
            .collect();
```

**File:** rs/nns/governance/api/src/types.rs (L3261-3265)
```rust
pub struct ListNeurons {
    /// The neurons to get information about. The "requested list"
    /// contains all of these neuron IDs.
    pub neuron_ids: Vec<u64>,
    /// If true, the "requested list" also contains the neuron ID of the
```

**File:** rs/nns/governance/canister/governance.did (L525-541)
```text
type ListNeurons = record {
  // These fields select neurons to be in the result set.
  neuron_ids : vec nat64;
  include_neurons_readable_by_caller : bool;

  // Only has an effect when include_neurons_readable_by_caller.
  include_empty_neurons_readable_by_caller : opt bool;

  // When a public neuron is a member of the result set, include it in the
  // full_neurons field (of ListNeuronsResponse). This does not affect which
  // neurons are part of the result set.
  include_public_neurons_in_full_neurons : opt bool;

  page_number: opt nat64;
  page_size: opt nat64;
  neuron_subaccounts: opt vec NeuronSubaccount;
};
```
