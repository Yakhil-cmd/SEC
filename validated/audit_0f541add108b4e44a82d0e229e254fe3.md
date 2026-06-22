### Title
Unbounded Iteration in `get_pending_proposals` Query Can Exhaust Instruction Limit, Making the Endpoint Unavailable - (File: rs/nns/governance/src/governance.rs)

---

### Summary

The NNS Governance canister exposes a `get_pending_proposals` query endpoint that iterates over the **entire** `heap_data.proposals` map without any limit or pagination. Because the proposals map is theoretically unbounded and any user with a staked neuron can submit proposals, a sufficiently large proposals map will cause this query to exhaust the IC instruction limit and return an error to all callers. This makes the endpoint permanently unavailable until the map shrinks via garbage collection.

---

### Finding Description

`get_pending_proposals` in `rs/nns/governance/src/governance.rs` performs an unbounded full-scan of `self.heap_data.proposals`:

```rust
self.heap_data
    .proposals
    .values()
    .filter(|data| data.status() == ProposalStatus::Open)
    .map(|data| {
        proposal_data_to_info(
            data,
            ProposalDisplayOptions::for_get_pending_proposals(...),
            &caller_neurons,
            now,
            self.voting_period_seconds(),
        )
    })
    .collect()
``` [1](#0-0) 

The `.filter()` is lazy but still visits **every** entry in the map to check its status. For proposals that pass the filter (open proposals), `proposal_data_to_info` is called, which processes the proposal's `ballots` map — one entry per neuron. With hundreds of thousands of neurons on the NNS, each open proposal's ballot map is large.

This function is registered as a `#[query]` endpoint, callable by any unprivileged user with no arguments: [2](#0-1) 

Query calls on the IC execute within a fixed instruction limit with no Deterministic Time Slicing (DTS) fallback. There is no pagination, no `limit` parameter, and no early-exit mechanism in `get_pending_proposals`.

The `proposals` field is a `BTreeMap<u64, ProposalData>` that accumulates entries over the lifetime of the canister: [3](#0-2) 

---

### Impact Explanation

When the proposals map grows large enough that iterating it (plus processing open proposals' ballot maps) exceeds the query instruction limit, every call to `get_pending_proposals` returns an instruction-limit error. This makes the endpoint permanently unavailable until the map shrinks via GC.

The Rosetta API's `pending_proposals()` method directly calls this endpoint: [4](#0-3) 

Unavailability of `get_pending_proposals` breaks the Rosetta API's ability to report pending governance proposals, disrupting ICP exchange integrations and governance tooling that depend on this endpoint.

---

### Likelihood Explanation

Any principal that has staked ICP and created a neuron can submit proposals. The NNS governance has been running for years and has accumulated a large proposals map. While a GC mechanism removes settled proposals, it runs periodically and may lag behind a burst of submissions. The attack requires staking ICP and paying rejection fees, making it non-trivial but feasible for a motivated attacker. The NNS changelog itself acknowledges instruction-limit exhaustion as a real concern for unbounded neuron/proposal iteration: [5](#0-4) 

---

### Recommendation

Add a hard cap on the number of proposals returned by `get_pending_proposals`, analogous to the `MAX_LIST_PROPOSAL_RESULTS` cap already applied in `list_proposals`: [6](#0-5) 

Specifically:
1. Apply `.take(MAX_PENDING_PROPOSALS_RESULTS)` before `.collect()` in `get_pending_proposals`.
2. Alternatively, maintain a dedicated in-memory index of open proposal IDs so the function does not need to scan the entire proposals map.
3. Ensure the proposals GC runs frequently enough to bound the map size.

---

### Proof of Concept

1. An attacker stakes ICP to create a neuron.
2. The attacker submits a large number of proposals (e.g., `Motion` proposals) in rapid succession, keeping them open by not voting them to conclusion.
3. Each open proposal's `ProposalData` contains a `ballots` map with one entry per neuron (hundreds of thousands on mainnet).
4. Any caller (including the Rosetta API) invokes `get_pending_proposals`.
5. The query iterates the full proposals map, processes each open proposal's ballot map, and exhausts the IC query instruction limit.
6. The endpoint returns `CanisterInstructionLimitExceeded` to all callers, making it permanently unavailable until GC reduces the map size. [7](#0-6) [2](#0-1)

### Citations

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

**File:** rs/nns/governance/src/governance.rs (L3623-3627)
```rust
        let limit = if req.limit == 0 || req.limit > MAX_LIST_PROPOSAL_RESULTS {
            MAX_LIST_PROPOSAL_RESULTS
        } else {
            req.limit
        } as usize;
```

**File:** rs/nns/governance/canister/canister.rs (L373-377)
```rust
#[query]
fn get_pending_proposals(req: Option<GetPendingProposalsRequest>) -> Vec<ProposalInfo> {
    debug_log("get_pending_proposals");
    with_governance(|governance| governance.get_pending_proposals(&caller(), req))
}
```

**File:** rs/nns/governance/api/src/types.rs (L2937-2939)
```rust
    pub neurons: BTreeMap<u64, Neuron>,
    /// Proposals.
    pub proposals: BTreeMap<u64, ProposalData>,
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

**File:** rs/nns/governance/CHANGELOG.md (L655-675)
```markdown
        * Distribute rewards is moved to timer, and has a mechanism to distribute in batches in
          multiple messages.
        * Unstaking maturity task has a limit of 100 neurons per message, which prevents it from
          exceeding instruction limit.
        * The execution of `ApproveGenesisKyc` proposals have a limit of 1000 neurons, above which
          the proposal will fail.
        * More benchmarks were added.
* Enable timer task metrics for better observability.

## Changed

* Voting Rewards will be scheduled by a timer instead of by heartbeats.
* Unstaking maturity task will be processing up to 100 neurons in a single message, to avoid
  exceeding the instruction limit in a single execution.
* Voting Rewards will be distributed asynchronously in the background after being calculated.
    * This will allow rewards to be compatible with neurons being stored in Stable Memory.
* Ramp up the failure rate of _pb method to 0.7 again.

## Fixed

* Avoid applying `approve_genesis_kyc` to an unbounded number of neurons, but at most 1000 neurons.
```
