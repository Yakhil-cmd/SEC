### Title
Unbounded Iteration Over All Proposals in `get_pending_proposals` Query - (`rs/nns/governance/src/governance.rs`)

### Summary
The NNS Governance canister exposes a publicly callable `get_pending_proposals` query that iterates over the **entire** `heap_data.proposals` map — including all historically finalized proposals — without any bound or pagination. As the NNS accumulates proposals over time, this iteration grows unboundedly, mirroring the `massUpdatePools()` pattern from the reference report. Any anonymous or unprivileged caller can invoke this query, and if the proposals map grows large enough, the query will exhaust the per-message instruction limit and become permanently unavailable.

### Finding Description
`get_pending_proposals` is registered as a `#[query]` endpoint in the NNS Governance canister: [1](#0-0) 

Its implementation iterates unconditionally over every entry in `self.heap_data.proposals` — a `BTreeMap` that accumulates all proposals (open and finalized) over the lifetime of the NNS — and only then filters for `ProposalStatus::Open`: [2](#0-1) 

The filter `data.status() == ProposalStatus::Open` is applied **after** the full map traversal, meaning the O(n) cost is paid over the total number of proposals ever submitted, not just the currently open ones. There is no `take(limit)`, no pagination, and no early exit.

While `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` caps the number of simultaneously open proposals, it places no bound on the total size of `heap_data.proposals`: [3](#0-2) 

The expensive per-proposal work (`proposal_data_to_info`, ballot filtering) is bounded by open proposals, but the raw map traversal to find them is not. The Rosetta API client calls this endpoint directly: [4](#0-3) 

### Impact Explanation
If `heap_data.proposals` grows large enough that iterating it exhausts the IC per-message instruction limit, the `get_pending_proposals` query will permanently return an instruction-limit error to every caller. This constitutes a **cycles/resource accounting DoS** of the query endpoint. Downstream consumers — including the Rosetta API, governance dashboards, and any tooling that polls pending proposals — would lose the ability to enumerate open proposals. The NNS governance canister itself would not be halted, but this critical read path would be permanently broken without a canister upgrade.

### Likelihood Explanation
The NNS has been running since 2021 and accumulates proposals continuously. The proposals map is stored in heap memory and is not pruned after finalization. As the NNS ages, the map grows monotonically. The query is callable by any anonymous principal with no rate limiting. No privileged access, key compromise, or majority attack is required — the condition is reached through normal protocol operation.

### Recommendation
Replace the full-map traversal with a bounded, paginated approach. Maintain a separate index of open proposal IDs (e.g., a `BTreeSet<u64>`) so that `get_pending_proposals` only touches open proposals directly, rather than scanning all historical proposals. Alternatively, add a `limit` parameter and return at most `MAX_LIST_PROPOSAL_RESULTS` entries per call, consistent with the existing `list_proposals` endpoint: [5](#0-4) 

### Proof of Concept
1. Observe that `get_pending_proposals` iterates `self.heap_data.proposals.values()` with no bound.
2. Note that the NNS proposals map grows monotonically (proposals are never pruned from heap state after finalization).
3. Any anonymous caller can invoke `get_pending_proposals` as a query call.
4. As the map grows to tens of thousands of entries, the per-query instruction cost grows proportionally.
5. Once the instruction limit is exceeded, every call to `get_pending_proposals` returns an error, permanently breaking the endpoint until a canister upgrade restructures the data access pattern. [6](#0-5)

### Citations

**File:** rs/nns/governance/canister/canister.rs (L373-377)
```rust
#[query]
fn get_pending_proposals(req: Option<GetPendingProposalsRequest>) -> Vec<ProposalInfo> {
    debug_log("get_pending_proposals");
    with_governance(|governance| governance.get_pending_proposals(&caller(), req))
}
```

**File:** rs/nns/governance/src/governance.rs (L242-247)
```rust
/// The maximum number results returned by the method `list_proposals`.
pub const MAX_LIST_PROPOSAL_RESULTS: u32 = 100;

/// The maximum number of neurons returned by `list_neurons`
pub const MAX_LIST_NEURONS_RESULTS: usize = 50;

```

**File:** rs/nns/governance/src/governance.rs (L3469-3495)
```rust
    pub fn get_pending_proposals(
        &self,
        caller: &PrincipalId,
        req: Option<GetPendingProposalsRequest>,
    ) -> Vec<ProposalInfo> {
        let now = self.env.now();
        let caller_neurons = self.get_neuron_ids_by_principal(caller);
        let return_self_describing_action = req
            .and_then(|r| r.return_self_describing_action)
            .unwrap_or(false);
        self.heap_data
            .proposals
            .values()
            .filter(|data| data.status() == ProposalStatus::Open)
            .map(|data| {
                proposal_data_to_info(
                    data,
                    ProposalDisplayOptions::for_get_pending_proposals(
                        return_self_describing_action,
                    ),
                    &caller_neurons,
                    now,
                    self.voting_period_seconds(),
                )
            })
            .collect()
    }
```

**File:** rs/rosetta-api/icp/src/ledger_client.rs (L375-396)
```rust
    async fn pending_proposals(&self) -> Result<Vec<ProposalInfo>, ApiError> {
        if self.offline {
            return Err(ApiError::NotAvailableOffline(false, Details::default()));
        }
        let agent = &self.canister_access.as_ref().unwrap().agent;
        let arg = Encode!().unwrap();
        let bytes = agent
            .query(
                &self.governance_canister_id.get().0,
                "get_pending_proposals",
            )
            .with_arg(arg)
            .call()
            .await
            .map_err(|e| ApiError::invalid_request(format!("{e}")))?;
        Decode!(bytes.as_slice(), Vec<ProposalInfo>).map_err(|err| {
            ApiError::InvalidRequest(
                false,
                Details::from(format!("Could not decode PendingProposals response: {err}")),
            )
        })
    }
```
