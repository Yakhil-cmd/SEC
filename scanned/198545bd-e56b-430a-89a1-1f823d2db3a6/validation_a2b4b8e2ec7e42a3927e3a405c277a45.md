### Title
Unbounded Iteration Over All Known Neurons in `list_known_neurons` Query - (File: `rs/nns/governance/src/governance.rs`)

### Summary
The NNS Governance canister's `list_known_neurons` query iterates over **all** known neurons in a single call with no pagination or result-size cap. As the number of known neurons grows (each added via a governance proposal), the instruction cost of this query grows linearly and unboundedly, eventually causing the query to exceed the IC's per-message instruction limit and trap, permanently denying service to any caller of `list_known_neurons`.

### Finding Description
The public query endpoint `list_known_neurons` in the NNS Governance canister calls `self.neuron_store.list_known_neuron_ids()`, which iterates over the entire `known_neuron_name_to_id` stable BTree map and collects all entries into a `Vec`. It then performs a second full pass, calling `self.neuron_store.with_neuron(&neuron_id, ...)` for every returned ID to hydrate each `KnownNeuron` struct. The result is returned as a single unbounded `Vec<KnownNeuron>` with no limit, no pagination, and no early-exit.

The canister-facing handler:

```rust
#[query]
fn list_known_neurons() -> ListKnownNeuronsResponse {
    debug_log("list_known_neurons");
    let response = governance().list_known_neurons();
    ListKnownNeuronsResponse::from(response)
}
```

calls directly into:

```rust
pub fn list_known_neurons(&self) -> ListKnownNeuronsResponse {
    let known_neurons: Vec<KnownNeuron> = self
        .neuron_store
        .list_known_neuron_ids()   // full scan of stable BTree map
        .into_iter()
        .flat_map(|neuron_id| {
            self.neuron_store
                .with_neuron(&neuron_id, |n| KnownNeuron { ... })
                ...
        })
        .collect();
    ListKnownNeuronsResponse { known_neurons }
}
```

`list_known_neuron_ids` itself does:

```rust
pub fn list_known_neuron_ids(&self) -> Vec<NeuronId> {
    self.known_neuron_name_to_id
        .iter()
        .map(|(_name, neuron_id)| neuron_id)
        .collect()
}
```

There is no cap on the number of known neurons that can be registered. Each `RegisterKnownNeuron` governance proposal (which any neuron holder can submit and which passes by majority vote) adds one entry to the stable BTree map. There is no `MAX_KNOWN_NEURONS` constant anywhere in the codebase.

By contrast, `list_neurons` and `list_proposals` both enforce a hard `MAX_LIST_NEURONS_RESULTS` / `MAX_LIST_PROPOSAL_RESULTS` cap and support pagination. `list_known_neurons` has neither.

### Impact Explanation
Once the number of known neurons grows large enough that iterating and hydrating all of them in a single query call exceeds the IC's per-query instruction limit (~5 billion instructions for query calls), every call to `list_known_neurons` will trap. The endpoint becomes permanently unavailable. This is a **cycles/resource accounting bug** causing **denial of service** of a core governance query endpoint. Downstream consumers that depend on `list_known_neurons` (e.g., the ICP Rosetta API at `rs/rosetta-api/icp/src/ledger_client.rs` which calls it unconditionally) would also break.

### Likelihood Explanation
Known neurons are added via NNS governance proposals. While the current number is small (tens), there is no protocol-level cap preventing growth. As the NNS matures and more organizations seek visibility, the count can grow. Additionally, a motivated attacker with sufficient voting power could pass many `RegisterKnownNeuron` proposals to deliberately inflate the count. The vulnerability is latent but structurally guaranteed to trigger at scale.

### Recommendation
1. Introduce a hard cap `MAX_KNOWN_NEURONS` (e.g., 1,000) enforced at proposal execution time in `KnownNeuron::execute` / `KnownNeuron::validate`.
2. Add pagination to `list_known_neurons` analogous to `list_neurons` (add `before_neuron_id` and `limit` parameters to the request type and enforce `MAX_LIST_KNOWN_NEURONS_RESULTS`).

### Proof of Concept

**Entry path (unprivileged query caller):**
1. Any anonymous or authenticated principal sends a query call to the NNS Governance canister (`rrkah-fqaaa-aaaaa-aaaaq-cai`) invoking `list_known_neurons` (no arguments required).
2. The canister handler at `rs/nns/governance/canister/canister.rs:438` dispatches to `governance().list_known_neurons()`.
3. `list_known_neurons` at `rs/nns/governance/src/governance.rs:1737` calls `self.neuron_store.list_known_neuron_ids()`.
4. `list_known_neuron_ids` at `rs/nns/governance/src/neuron_store.rs:595` calls `indexes.known_neuron().list_known_neuron_ids()`.
5. `KnownNeuronIndex::list_known_neuron_ids` at `rs/nns/governance/src/known_neuron_index.rs:120` iterates the entire `known_neuron_name_to_id` stable BTree map with no bound.
6. Back in `list_known_neurons`, a second full pass hydrates each neuron from stable storage.
7. With sufficiently many known neurons, the total instruction count exceeds the query limit and the call traps.

**Root cause files:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

**No cap on known neuron count at registration:** [5](#0-4) 

**Contrast: `list_neurons` enforces a page size cap; `list_known_neurons` does not:** [6](#0-5)

### Citations

**File:** rs/nns/governance/canister/canister.rs (L437-442)
```rust
#[query]
fn list_known_neurons() -> ListKnownNeuronsResponse {
    debug_log("list_known_neurons");
    let response = governance().list_known_neurons();
    ListKnownNeuronsResponse::from(response)
}
```

**File:** rs/nns/governance/src/governance.rs (L1631-1634)
```rust
        let page_number = page_number.unwrap_or(0);
        let page_size = page_size
            .unwrap_or(MAX_LIST_NEURONS_RESULTS as u64)
            .min(MAX_LIST_NEURONS_RESULTS as u64);
```

**File:** rs/nns/governance/src/governance.rs (L1737-1762)
```rust
    pub fn list_known_neurons(&self) -> ListKnownNeuronsResponse {
        // This should be migrated to known neuron index before migrating any neuron to stable storage.
        let known_neurons: Vec<KnownNeuron> = self
            .neuron_store
            .list_known_neuron_ids()
            .into_iter()
            // Flat map to discard neuron_not_found errors here, which we cannot handle here
            // and indicates a problem with NeuronStore
            .flat_map(|neuron_id| {
                self.neuron_store
                    .with_neuron(&neuron_id, |n| KnownNeuron {
                        id: Some(n.id()),
                        known_neuron_data: n.known_neuron_data().cloned(),
                    })
                    .map_err(|e| {
                        println!(
                            "Error while listing known neurons.  Neuron disappeared: {:?}",
                            e
                        );
                        e
                    })
            })
            .collect();

        ListKnownNeuronsResponse { known_neurons }
    }
```

**File:** rs/nns/governance/src/neuron_store.rs (L594-597)
```rust
    /// List all neuron ids of known neurons
    pub fn list_known_neuron_ids(&self) -> Vec<NeuronId> {
        with_stable_neuron_indexes(|indexes| indexes.known_neuron().list_known_neuron_ids())
    }
```

**File:** rs/nns/governance/src/known_neuron_index.rs (L119-125)
```rust
    /// Lists all known neuron ids.
    pub fn list_known_neuron_ids(&self) -> Vec<NeuronId> {
        self.known_neuron_name_to_id
            .iter()
            .map(|(_name, neuron_id)| neuron_id)
            .collect()
    }
```

**File:** rs/nns/governance/src/proposals/register_known_neuron.rs (L36-146)
```rust
    pub fn validate(&self, neuron_store: &NeuronStore) -> Result<(), GovernanceError> {
        let neuron_id = self.id.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                "No neuron ID specified in the request to register a known neuron.",
            )
        })?;

        // Check that the neuron exists
        if !neuron_store.contains(neuron_id) {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotFound,
                format!("Neuron {} not found", neuron_id.id),
            ));
        }

        let known_neuron_data = self.known_neuron_data.as_ref().ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                "No known neuron data specified in the register neuron request.",
            )
        })?;

        // Validate name length
        if known_neuron_data.name.is_empty() {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                "The neuron's name is empty.",
            ));
        }
        if known_neuron_data.name.len() > KNOWN_NEURON_NAME_MAX_LEN {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!(
                    "The maximum number of bytes for a neuron's name, which is {}, \
                    has been exceeded. Current length: {}",
                    KNOWN_NEURON_NAME_MAX_LEN,
                    known_neuron_data.name.len()
                ),
            ));
        }

        // Validate description length
        if let Some(description) = &known_neuron_data.description
            && description.len() > KNOWN_NEURON_DESCRIPTION_MAX_LEN
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!(
                    "The maximum number of bytes for a neuron's description, which is {}, \
                        has been exceeded. Current length: {}",
                    KNOWN_NEURON_DESCRIPTION_MAX_LEN,
                    description.len()
                ),
            ));
        }

        // Validate links
        if known_neuron_data.links.len() > MAX_KNOWN_NEURON_LINKS {
            return Err(GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!(
                    "The maximum number of links, which is {}, has been exceeded. \
                    Current number of links: {}",
                    MAX_KNOWN_NEURON_LINKS,
                    known_neuron_data.links.len()
                ),
            ));
        }
        for (index, link) in known_neuron_data.links.iter().enumerate() {
            validate_url(link, 0, MAX_KNOWN_NEURON_LINK_SIZE, "links", None).map_err(|error| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!("Link at index {index} is not valid. Error: {error}"),
                )
            })?;
        }

        // Validate committed_topics for duplicates
        let mut topic_set = HashSet::new();
        for (index, topic) in known_neuron_data.committed_topics.iter().enumerate() {
            if !topic_set.insert(topic) {
                return Err(GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!(
                        "Duplicate topic found in committed_topics at index {}: {:?}",
                        index, topic
                    ),
                ));
            }
        }

        // Check that the name is not already used by another known neuron
        // Allow registration if:
        // - No existing known neuron has this name (None), OR
        // - An existing known neuron has this name but it's the same neuron ID (clobbering OK)
        if let Some(existing_neuron_id) =
            neuron_store.known_neuron_id_by_name(&known_neuron_data.name)
            && existing_neuron_id != neuron_id
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "The name '{}' already belongs to a different known neuron with ID {}",
                    known_neuron_data.name, existing_neuron_id.id
                ),
            ));
        }

        Ok(())
    }
```
