### Title
Unbounded Iteration Over All Proposals in `get_pending_proposals` Query Causes Instruction-Limit Exhaustion - (File: rs/nns/governance/src/governance.rs)

### Summary
The NNS Governance canister's `get_pending_proposals` query endpoint iterates over the entire `proposals` BTreeMap without any bound or pagination, processing every proposal (including all settled/executed/rejected ones) to filter for open ones. Because the proposals map grows unboundedly over the canister's lifetime and each open proposal's ballot set scales with the total neuron count (up to 500 K), any unprivileged caller can trigger this query and, once the map is large enough, cause it to exceed the IC's per-query instruction limit (5 billion instructions), permanently breaking the endpoint.

### Finding Description

`get_pending_proposals` is exposed as a `#[query]` canister method in `rs/nns/governance/canister/canister.rs`:

```rust
#[query]
fn get_pending_proposals(req: Option<GetPendingProposalsRequest>) -> Vec<ProposalInfo> {
    with_governance(|governance| governance.get_pending_proposals(&caller(), req))
}
```

The implementation in `rs/nns/governance/src/governance.rs` at lines 3469–3494 iterates over **all** entries in `self.heap_data.proposals` (a `BTreeMap<u64, ProposalData>`) with no limit:

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
```

The `.filter()` is lazy but the `.values()` iterator still visits every entry in the map. The proposals map is never bounded: settled proposals accumulate until the periodic GC runs (`latest_gc_timestamp_seconds`), and GC is not guaranteed to keep the map small. In contrast, `list_proposals` (the paginated sibling) correctly applies `.take(limit)` after filtering.

Additionally, `proposal_data_to_info` for each open proposal processes the full ballot map (one entry per eligible neuron). With up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` open proposals and up to 500 K neurons, the per-call instruction cost is:

```
O(|proposals_total|) + O(open_proposals × neurons_per_proposal)
```

Both terms can grow large enough to exceed the 5-billion-instruction query limit.

A secondary unbounded-scan pattern exists in `get_node_provider` (lines 7518–7535), which does a linear scan over `heap_data.node_providers: Vec<NodeProvider>` with an acknowledged TODO comment:

```rust
// TODO(NNS1-1168): More efficient Node Provider lookup
self.heap_data
    .node_providers
    .iter()
    .find(|np| np.id.as_ref() == Some(node_provider_id))
```

The same pattern appears in `update_node_provider` (lines 6896–6900) and `ValidAddNodeProvider::validate` / `ValidRemoveNodeProvider::find_existing_node_provider_position` in `rs/nns/governance/src/proposals/add_or_remove_node_provider.rs` (lines 150–152, 218–220).

### Impact Explanation

When the proposals map grows large (many settled proposals awaiting GC), any caller invoking `get_pending_proposals` will receive an instruction-limit trap error. This breaks:
- The Rosetta API's `pending_proposals()` path (`rs/rosetta-api/icp/src/ledger_client.rs` lines 375–396), which calls this query directly
- Any governance dashboard or integration relying on this endpoint
- The `get_pending_proposals` canister method itself becomes permanently unavailable until a canister upgrade or GC cycle reduces the map size

**Impact: Medium** — availability loss of a publicly relied-upon governance query endpoint.

### Likelihood Explanation

The proposals map grows naturally over the canister's lifetime. The NNS Governance canister has been running for years and has processed tens of thousands of proposals. GC is periodic and not guaranteed to keep the map small. No attacker action is required beyond waiting; however, a motivated attacker with ICP stake could accelerate growth by submitting many proposals. The instruction limit for query calls (5 billion) is fixed and cannot be increased per-call.

**Likelihood: Medium** — the condition arises naturally over time; no privileged access is required to trigger the query.

### Recommendation

1. **Add a limit/pagination to `get_pending_proposals`**: Apply `.take(MAX_PENDING_PROPOSALS_RESULTS)` analogously to how `list_proposals` uses `MAX_LIST_PROPOSAL_RESULTS` with `.take(limit)`.
2. **Iterate only over open proposals**: Maintain a separate index of open proposal IDs (e.g., a `BTreeSet<u64>`) so the function does not scan the entire proposals map.
3. **Replace `node_providers: Vec<NodeProvider>` with a `BTreeMap<PrincipalId, NodeProvider>`**: Eliminates the O(n) linear scans in `get_node_provider`, `update_node_provider`, and `list_node_providers`.

### Proof of Concept

**Entry path (unprivileged):**
1. Any principal (including anonymous) calls `get_pending_proposals` on the NNS Governance canister (`rrkah-fqaaa-aaaaa-aaaaq-cai`) as a query call — no authentication required.
2. The canister iterates over all entries in `self.heap_data.proposals`.
3. Once `|proposals_total|` is large enough (or `open_proposals × neurons` is large enough), the query exceeds 5 billion instructions and traps with `CanisterError: Canister exceeded the limit of 5000000000 instructions`.
4. All callers of `get_pending_proposals` — including the Rosetta API — receive errors until the proposals map shrinks via GC.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** rs/nns/governance/src/governance.rs (L3586-3641)
```rust
    pub fn list_proposals(
        &self,
        caller: &PrincipalId,
        req: ListProposalInfoRequest,
    ) -> ListProposalInfoResponse {
        let exclude_topic: HashSet<i32> = req.exclude_topic.iter().cloned().collect();
        let include_reward_status: HashSet<i32> =
            req.include_reward_status.iter().cloned().collect();
        let include_status: HashSet<i32> = req.include_status.iter().cloned().collect();
        let caller_neurons = self.get_neuron_ids_by_principal(caller);
        let return_self_describing_action = req.return_self_describing_action.unwrap_or(false);
        let now = self.env.now();
        let proposal_matches_request = |data: &ProposalData| -> bool {
            let topic = data.topic();
            let voting_period_seconds = self.voting_period_seconds()(topic);
            // Filter out proposals by topic.
            if exclude_topic.contains(&(topic as i32)) {
                return false;
            }
            // Filter out proposals by reward status.
            if !(include_reward_status.is_empty()
                || include_reward_status
                    .contains(&(data.reward_status(now, voting_period_seconds) as i32)))
            {
                return false;
            }
            // Filter out proposals by status.
            if !(include_status.is_empty() || include_status.contains(&(data.status() as i32))) {
                return false;
            }
            // Filter out proposals by the visibility of the caller principal
            // when include_all_manage_neuron_proposals is false. When
            // include_all_manage_neuron_proposals is true the proposal is
            // always included.
            req.include_all_manage_neuron_proposals.unwrap_or(false)
                || self.proposal_is_visible_to_neurons(data, &caller_neurons)
        };
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
```

**File:** rs/nns/governance/src/governance.rs (L7518-7535)
```rust
    /// Return the given Node Provider, if it exists
    pub fn get_node_provider(
        &self,
        node_provider_id: &PrincipalId,
    ) -> Result<NodeProvider, GovernanceError> {
        // TODO(NNS1-1168): More efficient Node Provider lookup
        self.heap_data
            .node_providers
            .iter()
            .find(|np| np.id.as_ref() == Some(node_provider_id))
            .cloned()
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::NotFound,
                    format!("Node Provider {node_provider_id} is not known by the NNS"),
                )
            })
    }
```

**File:** rs/nns/governance/src/proposals/add_or_remove_node_provider.rs (L148-161)
```rust
impl ValidAddNodeProvider {
    pub fn validate(&self, node_providers: &[NodeProvider]) -> Result<(), GovernanceError> {
        let already_exists = node_providers
            .iter()
            .any(|node_provider| node_provider.id == Some(self.id));
        if already_exists {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("NodeProvider with id {} already exists", self.id),
            ));
        }

        Ok(())
    }
```

**File:** rs/nns/governance/src/proposals/add_or_remove_node_provider.rs (L214-230)
```rust
    fn find_existing_node_provider_position(
        &self,
        node_providers: &[NodeProvider],
    ) -> Result<usize, GovernanceError> {
        node_providers
            .iter()
            .position(|node_provider| node_provider.id == Some(self.id))
            .ok_or_else(|| {
                GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    format!(
                        "AddOrRemoveNodeProvider ToRemove must target an existing Node Provider but targeted {}",
                        self.id
                    ),
                )
            })
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
