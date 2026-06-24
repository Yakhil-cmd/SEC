### Title
Unbounded Iteration in `list_known_neurons` Exhausts Query Instruction Limit - (File: `rs/nns/governance/src/governance.rs`)

### Summary
The NNS Governance canister exposes a public `#[query]` endpoint `list_known_neurons` that iterates over every known neuron in stable storage without any pagination or result limit. As the number of known neurons grows, this query will eventually exhaust the Internet Computer's per-query instruction limit and trap, permanently disabling the endpoint for all callers.

### Finding Description
`list_known_neurons` in `rs/nns/governance/src/governance.rs` calls `list_known_neuron_ids()`, which iterates over the entire `known_neuron_name_to_id` `StableBTreeMap` to collect all IDs, then performs a separate stable-storage read (`with_neuron`) for every ID to assemble the full `KnownNeuron` struct: [1](#0-0) 

The underlying `list_known_neuron_ids` in `KnownNeuronIndex` performs an unbounded `.iter()` over the entire stable map with no `take` or limit: [2](#0-1) 

This function is wired directly to the public `#[query]` canister endpoint with no access control: [3](#0-2) 

There is no `limit`, `from`, or `to` parameter in the request type — the endpoint always returns the complete set.

### Impact Explanation
On the Internet Computer, query calls are bounded by a per-message instruction limit (currently 5 billion instructions). Each `with_neuron` call performs a stable-storage read, which is significantly more expensive in instructions than a heap read. As the number of registered known neurons grows — each added via a successful `RegisterKnownNeuron` governance proposal — the cumulative instruction cost of iterating and reading all of them will eventually exceed the query instruction limit. When that threshold is crossed, every call to `list_known_neurons` traps unconditionally, permanently breaking the endpoint. This also breaks the Rosetta API's `list_known_neurons` call path: [4](#0-3) 

### Likelihood Explanation
Any unprivileged principal can call `list_known_neurons` as a query. The number of known neurons is unbounded by protocol — there is no cap enforced in the governance code. Each successful `RegisterKnownNeuron` NNS proposal permanently adds one entry to the stable map. The NNS has been live for years and the known-neuron set grows over time. The stable-storage iteration cost makes the instruction budget the binding constraint well before the response size limit.

### Recommendation
Add pagination parameters (`limit`, `start_after`) to `list_known_neurons`, analogous to how `list_neurons` and `list_proposals` already handle large collections. The `KnownNeuronIndex` already uses a `StableBTreeMap` that supports efficient range queries, so a cursor-based approach (e.g., `range(start_after..)` + `.take(limit)`) is straightforward to implement. [5](#0-4) 

### Proof of Concept
1. Register a large number of known neurons via successive `RegisterKnownNeuron` governance proposals (each proposal adds one entry to the `known_neuron_name_to_id` stable map).
2. Call `list_known_neurons` as any anonymous principal.
3. Once the number of known neurons is large enough that the cumulative cost of `StableBTreeMap::iter()` + N × `with_neuron` stable reads exceeds the query instruction limit, the call traps with an instruction-limit-exceeded error.
4. The endpoint becomes permanently unavailable to all callers, including the Rosetta API integration. [6](#0-5)

### Citations

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

**File:** rs/nns/governance/src/known_neuron_index.rs (L29-34)
```rust
impl<M: Memory> KnownNeuronIndex<M> {
    pub fn new(memory: M) -> Self {
        Self {
            known_neuron_name_to_id: StableBTreeMap::init(memory),
        }
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

**File:** rs/nns/governance/canister/canister.rs (L437-442)
```rust
#[query]
fn list_known_neurons() -> ListKnownNeuronsResponse {
    debug_log("list_known_neurons");
    let response = governance().list_known_neurons();
    ListKnownNeuronsResponse::from(response)
}
```

**File:** rs/rosetta-api/icp/src/ledger_client.rs (L426-448)
```rust
    async fn list_known_neurons(&self) -> Result<Vec<KnownNeuron>, ApiError> {
        if self.offline {
            return Err(ApiError::NotAvailableOffline(false, Details::default()));
        }
        let agent = &self.canister_access.as_ref().unwrap().agent;
        let arg = Encode!().unwrap();
        let bytes = agent
            .query(&self.governance_canister_id.get().0, "list_known_neurons")
            .with_arg(arg)
            .call()
            .await
            .map_err(|e| ApiError::invalid_request(format!("{e}")))?;
        Decode!(bytes.as_slice(), ListKnownNeuronsResponse)
            .map_err(|err| {
                ApiError::InvalidRequest(
                    false,
                    Details::from(format!(
                        "Could not decode ListKnownNeuronsResponse response: {err}"
                    )),
                )
            })
            .map(|res| res.known_neurons)
    }
```
