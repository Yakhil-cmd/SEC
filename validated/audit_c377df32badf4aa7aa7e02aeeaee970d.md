### Title
Unbounded Iteration Over All Proposals in `get_pending_proposals` Query Causes Instruction-Limit Denial of Service - (File: rs/nns/governance/src/governance.rs)

### Summary

The NNS governance canister exposes a `get_pending_proposals` query endpoint that iterates over the **entire** proposals map without any limit or pagination. As the NNS accumulates proposals over time, and as each open proposal carries a ballot map with entries for every voting neuron, this function's per-call instruction cost grows without bound. Any unprivileged caller can trigger this query; once the instruction limit is exceeded, the function permanently reverts for all callers.

### Finding Description

`get_pending_proposals` is registered as a `#[query]` method in the NNS governance canister: [1](#0-0) 

Its implementation unconditionally iterates over every entry in `self.heap_data.proposals`, filtering for `ProposalStatus::Open`, and then calls `proposal_data_to_info` on each matching entry: [2](#0-1) 

There is no `limit`, no `before_proposal` cursor, and no start/stop index. The cost of a single call is:

1. **O(total proposals)** — the entire map is scanned to find open ones.
2. **O(neurons per proposal)** — `proposal_data_to_info` filters the ballot map (one entry per voting neuron) against the caller's neuron set for each open proposal. NNS proposals routinely carry ballots for hundreds of thousands of neurons.

The `list_proposals` sibling correctly enforces `MAX_LIST_PROPOSAL_RESULTS` and supports `before_proposal` pagination: [3](#0-2) 

`get_pending_proposals` has no equivalent guard.

### Impact Explanation

On the Internet Computer, query calls are subject to a per-message instruction limit (currently ~5 billion instructions on application subnets). Once the combined cost of scanning all proposals and processing their ballot maps exceeds this limit, every call to `get_pending_proposals` traps with `CanisterInstructionLimitExceeded`. Because the proposals map only grows (purging is bounded and slow), the function becomes permanently unavailable to all callers — including the ICP Rosetta API, which depends on this endpoint to serve `get_pending_proposals` to exchanges and wallets: [4](#0-3) 

### Likelihood Explanation

The NNS governance canister has been live since 2021 and already holds thousands of proposals. Open proposals (those still in the voting period) can number in the dozens to hundreds simultaneously. Each open proposal's ballot map contains an entry for every neuron that voted (directly or via following), which can be in the hundreds of thousands. The instruction cost per call is therefore already substantial and grows monotonically with NNS usage. No attacker action is required — normal NNS operation drives the system toward the limit.

### Recommendation

Apply the same fix used for `list_proposals`: add `limit` and `before_proposal` (or equivalent start/stop) parameters to `get_pending_proposals`, enforce a maximum page size, and return a cursor so callers can paginate. Alternatively, maintain a dedicated index of open proposal IDs so the function does not need to scan the full proposals map.

### Proof of Concept

1. Observe that `get_pending_proposals` calls `.values()` on the full proposals map with no `.take(limit)`: [5](#0-4) 

2. Contrast with `list_proposals`, which applies `.take(limit)` after filtering: [6](#0-5) 

3. Any unprivileged user can call `get_pending_proposals` as a query (no cycles attached, no authentication required): [1](#0-0) 

4. The Rosetta API calls this endpoint synchronously on every `pending_proposals` request, meaning exchange integrations break permanently once the limit is hit: [4](#0-3)

### Citations

**File:** rs/nns/governance/canister/canister.rs (L373-377)
```rust
#[query]
fn get_pending_proposals(req: Option<GetPendingProposalsRequest>) -> Vec<ProposalInfo> {
    debug_log("get_pending_proposals");
    with_governance(|governance| governance.get_pending_proposals(&caller(), req))
}
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

**File:** rs/nns/governance/src/governance.rs (L3623-3654)
```rust
        let limit = if req.limit == 0 || req.limit > MAX_LIST_PROPOSAL_RESULTS {
            MAX_LIST_PROPOSAL_RESULTS
        } else {
            req.limit
        } as usize;
        let proposals = &self.heap_data.proposals;
        // Proposals are stored in a sorted map. If 'before_proposal'
        // is provided, grab all proposals before that, else grab the
        // whole range.
        let proposals = if let Some(n) = req.before_proposal {
            proposals.range(..(n.id))
        } else {
            proposals.range(..)
        };
        // Now reverse the range, filter, and restrict to 'limit'.
        let proposals = proposals
            .rev()
            .filter(|(_, x)| proposal_matches_request(x))
            .take(limit)
            .map(|(_, proposal_data)| {
                proposal_data_to_info(
                    proposal_data,
                    ProposalDisplayOptions::for_list_proposals(
                        req.omit_large_fields.unwrap_or_default(),
                        return_self_describing_action,
                    ),
                    &caller_neurons,
                    now,
                    self.voting_period_seconds(),
                )
            })
            .collect();
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
