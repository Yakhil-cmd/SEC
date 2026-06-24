### Title
Unbounded `list_known_neurons` Query Will Fail as Known-Neuron Registry Grows - (File: rs/nns/governance/canister/canister.rs)

### Summary
The NNS Governance canister exposes a public query `list_known_neurons()` that returns the entire known-neuron registry in a single response with no pagination or size limit. As the registry grows via governance proposals, the query will exhaust the IC instruction limit and become permanently unavailable to integrators.

### Finding Description

`list_known_neurons` is a `#[query]` endpoint with no arguments and no pagination parameters:

```
list_known_neurons : () -> (ListKnownNeuronsResponse) query;
``` [1](#0-0) 

The canister handler collects and returns the full set unconditionally: [2](#0-1) 

The underlying `list_known_neurons` implementation performs two stable-storage operations per entry: a full sequential scan of the `known_neuron_name_to_id` `StableBTreeMap`, then a per-neuron lookup in the neuron store for each ID returned: [3](#0-2) 

The index scan itself iterates the entire stable map: [4](#0-3) 

Each `KnownNeuron` entry carries variable-length heap data (`name` up to 200 chars, `description` up to 3 000 chars, `links`, `committed_topics`), so both instruction cost and response byte size grow with the registry. There is no enforced cap on the total number of known neurons, and the `KnownNeuronIndex` is backed by a `StableBTreeMap` that can grow indefinitely. [5](#0-4) 

A secondary instance of the same pattern exists in `list_node_providers()`, which returns `&self.heap_data.node_providers` in full with no pagination: [6](#0-5) [7](#0-6) 

### Impact Explanation

IC query calls are bounded by a per-message instruction limit (currently ~5 billion instructions). Stable-memory reads are significantly more expensive per byte than heap reads. As the known-neuron count grows, the double stable-storage traversal (index scan + per-neuron fetch) will eventually exhaust the instruction budget, causing the query to trap with `CanisterInstructionLimitExceeded`. At that point the entire known-neuron registry becomes inaccessible via this endpoint. Integrators (dashboards, wallets, Rosetta) that rely on `list_known_neurons` — including the ICP Rosetta API which calls it directly — will receive permanent errors and must fall back to replaying governance proposal events to reconstruct the registry, which is operationally complex and error-prone. [8](#0-7) 

### Likelihood Explanation

Known neurons are registered through NNS governance proposals, so growth is gated by the proposal process. The current count is small (tens of neurons). However, there is no protocol-level cap, and as the IC ecosystem matures and more named neurons are registered, the registry will grow. The instruction cost scales with both the count and the size of each `KnownNeuronData` payload. The failure threshold is reachable without any adversarial action — ordinary organic growth is sufficient. The analogous `list_neurons` endpoint already hit this problem and was retrofitted with pagination (`page_number`/`page_size`), confirming the pattern is realistic. [9](#0-8) 

### Recommendation

1. Add `limit` and `start_after` (or `page_number`/`page_size`) parameters to `list_known_neurons`, mirroring the pagination retrofit applied to `list_neurons`.
2. Enforce a per-call cap (e.g., 100 entries) and return a continuation cursor so callers can page through the full set.
3. Apply the same fix to `list_node_providers`.
4. Consider adding a `known_neurons_count()` query so callers can determine how many pages to request.

### Proof of Concept

An unprivileged caller (anonymous principal) issues:

```
dfx canister --ic call rrkah-fqaaa-aaaaa-aaaaq-cai list_known_neurons '()'
```

With a sufficiently large known-neuron registry the replica returns:

```
Error: The replica returned a rejection error: reject code CanisterError,
reject message IC0515: Canister rrkah-fqaaa-aaaaa-aaaaq-cai exceeded the
instruction limit for single message execution.
```

No special privilege is required. The call path is:

1. Anonymous ingress query → NNS Governance canister `list_known_neurons` handler
2. `governance().list_known_neurons()` → `neuron_store.list_known_neuron_ids()` → full `StableBTreeMap` scan
3. Per-ID `with_neuron(...)` → second stable-storage read per entry
4. Collect + serialize → response [3](#0-2) [4](#0-3)

### Citations

**File:** rs/nns/governance/canister/governance.did (L1637-1637)
```text
  list_known_neurons : () -> (ListKnownNeuronsResponse) query;
```

**File:** rs/nns/governance/canister/canister.rs (L552-561)
```rust
#[query]
fn list_node_providers() -> ListNodeProvidersResponse {
    debug_log("list_node_providers");
    let node_providers = governance()
        .get_node_providers()
        .iter()
        .map(|np| NodeProvider::from(np.clone()))
        .collect::<Vec<_>>();
    ListNodeProvidersResponse { node_providers }
}
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

**File:** rs/nns/governance/src/governance.rs (L3363-3366)
```rust
    // Returns the set of currently registered node providers.
    pub fn get_node_providers(&self) -> &[NodeProvider] {
        &self.heap_data.node_providers
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

**File:** rs/nns/governance/api/src/types.rs (L3325-3332)
```rust
/// A response to "ListKnownNeurons"
#[derive(
    candid::CandidType, candid::Deserialize, serde::Serialize, Clone, PartialEq, Debug, Default,
)]
pub struct ListKnownNeuronsResponse {
    /// List of known neurons.
    pub known_neurons: Vec<KnownNeuron>,
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

**File:** rs/nns/governance/CHANGELOG.md (L778-793)
```markdown
### List Neurons Paging

Two new fields are added to the request, and one to the response.

The request now supports `page_size` and `page_number`. If `page_size` is greater than
`MAX_LIST_NEURONS_RESULTS` (currently 500), the API will treat it as `MAX_LIST_NEURONS_RESULTS`, and
continue procesisng the request. If `page_number` is None, the API will treat it as Some(0)

In the response, a field `total_pages_available` is available to tell the user how many
additional requests need to be made.

This will only affect neuron holders with more than 500 neurons, which is a small minority.

This allows neuron holders with many neurons to list all of their neurons, whereas before,
responses could be too large to be sent by the protocol.

```
