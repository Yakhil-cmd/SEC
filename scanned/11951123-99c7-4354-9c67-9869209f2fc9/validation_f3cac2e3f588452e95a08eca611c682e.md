### Title
Node-Count-Weighted Ballot Inflation in Root Proposal Voting Allows Single Operator to Exceed Fair Share of Byzantine Threshold — (File: `rs/nns/handlers/root/impl/src/root_proposals.rs`)

### Summary

The `vote_on_root_proposal_to_upgrade_governance_canister` function in the NNS root canister allows a single node operator principal to cast **multiple ballots** — one per node they operate — in a single call. The `is_byzantine_majority_yes` threshold check counts **total ballot entries** (one per node) rather than **unique operator principals**. A node operator controlling many NNS subnet nodes can therefore contribute a disproportionately large share of the `N - f` threshold, analogous to the reported pattern where different role types (operators vs. admins) are not properly separated in approval counting.

### Finding Description

The `GovernanceUpgradeRootProposal` stores ballots as `Vec<(PrincipalId, RootProposalBallot)>` — one entry per NNS subnet **node**, not per unique operator principal.

```rust
pub node_operator_ballots: Vec<(PrincipalId, RootProposalBallot)>,
```

During submission, the proposer's principal is inserted once **per node they operate**:

```rust
for node in nns_nodes {
    ...
    if node_operator_pid == caller {
        voted_on += 1;
        node_operator_ballots.push((node_operator_pid, RootProposalBallot::Yes));
    } else {
        node_operator_ballots.push((node_operator_pid, RootProposalBallot::Undecided));
    }
}
```

During voting, the same pattern applies — a voter's ballot is set for **every entry** matching their principal:

```rust
for (p, b) in &mut proposal.node_operator_ballots {
    if p == &caller {
        *b = ballot.clone();
        voted_on += 1;
    }
}
```

The Byzantine majority check counts **all ballot entries** (one per node), not unique principals:

```rust
fn is_byzantine_majority_yes(&self) -> bool {
    let num_nodes = self.node_operator_ballots.len();
    let max_faults = (num_nodes - 1) / 3;
    let votes_yes: usize = self
        .node_operator_ballots
        .iter()
        .map(|(_, b)| match b {
            RootProposalBallot::Yes => 1,
            _ => 0,
        })
        .sum();
    votes_yes >= (num_nodes - max_faults)
}
```

The design intent (per the doc comment) is that the security level should equal the subnet's own Byzantine fault tolerance. However, the threshold `N - f` where `N = node_operator_ballots.len()` is the **total node count**, while a single operator controlling `k` nodes contributes `k` yes-votes in a single call. This means an operator controlling a large fraction of NNS nodes can single-handedly satisfy a disproportionate share of the threshold — the same root cause as the reported "operator approvals equal to admin approvals" pattern: different role/weight classes (single-node operators vs. multi-node operators) are not separated in the approval count.

### Impact Explanation

The `vote_on_root_proposal_to_upgrade_governance_canister` endpoint, when called by a node operator controlling many NNS subnet nodes, allows that single principal to cast `k` yes-votes in one call. If `k` is large enough relative to `N - f`, a single operator can unilaterally satisfy the Byzantine majority threshold and trigger `change_canister(payload)` — which upgrades the NNS governance canister to an arbitrary WASM. This is the most sensitive privileged operation in the NNS: replacing the governance canister with a malicious WASM would give the attacker full control over the NNS.

The impact is: **unauthorized execution of the governance canister upgrade** by a single node operator who controls enough nodes, bypassing the intended multi-party Byzantine threshold.

### Likelihood Explanation

The NNS subnet currently has ~40 nodes. The Byzantine threshold requires `N - f = N - (N-1)/3` yes-votes. If a single operator controls, say, 15 of 40 nodes, they contribute 15 yes-votes in one call. The remaining threshold is `40 - 13 = 27`, so 15 votes alone is not sufficient. However, in a smaller NNS subnet configuration (e.g., during testing or a future reconfiguration), or if a single operator controls a larger fraction, the threshold can be met by fewer principals. The attack requires the caller to be a legitimate NNS node operator (verifiable from the registry), which limits the attacker pool but does not eliminate the risk. The entry path is a direct ingress call to the root canister's `vote_on_root_proposal_to_upgrade_governance_canister` update method — no privileged key or social engineering required beyond being a registered node operator.

### Recommendation

The ballot structure should be keyed by **unique operator principal**, not by node. The `is_byzantine_majority_yes` check should count **distinct principals** that voted yes, not total ballot entries. Alternatively, each unique operator principal should receive exactly one ballot entry regardless of how many nodes they operate, and the threshold should be computed over unique principals. This mirrors the fix described in the external report: separate counters per role/entity rather than aggregating all entries into a single count.

### Proof of Concept

1. Suppose the NNS subnet has 7 nodes, with node operators: A (3 nodes), B (1 node), C (1 node), D (1 node), E (1 node).
2. `N = 7`, `f = (7-1)/3 = 2`, threshold = `7 - 2 = 5` yes-votes.
3. Operator A submits a root proposal. At submission, `node_operator_ballots` contains 3 entries for A (all `Yes`) and 4 entries for B/C/D/E (`Undecided`). `votes_yes = 3`.
4. Operator B calls `vote_on_root_proposal_to_upgrade_governance_canister` with `Yes`. Now `votes_yes = 4`.
5. Operator C calls with `Yes`. Now `votes_yes = 5 >= 5 = num_nodes - max_faults`. `is_byzantine_majority_yes()` returns `true`.
6. The governance canister is upgraded with the payload from A's proposal — decided by only 3 distinct principals (A, B, C) out of 5 operators, not the intended Byzantine majority of distinct operators.

The root canister's `vote_on_root_proposal_to_upgrade_governance_canister` is callable by any ingress sender whose principal matches a registered NNS node operator. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L100-101)
```rust
    /// The ballots cast by node operators.
    pub node_operator_ballots: Vec<(PrincipalId, RootProposalBallot)>,
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L110-121)
```rust
    fn is_byzantine_majority_yes(&self) -> bool {
        let num_nodes = self.node_operator_ballots.len();
        let max_faults = (num_nodes - 1) / 3;
        let votes_yes: usize = self
            .node_operator_ballots
            .iter()
            .map(|(_, b)| match b {
                RootProposalBallot::Yes => 1,
                _ => 0,
            })
            .sum();
        votes_yes >= (num_nodes - max_faults)
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L228-239)
```rust
    for node in nns_nodes {
        total_votes += 1;
        let node_operator_pid =
            get_node_operator_pid_of_node(&node, subnet_membership_registry_version)
                .await
                .map_err(|e| format!("Error: {e:?}"))?;
        if node_operator_pid == caller {
            voted_on += 1;
            node_operator_ballots.push((node_operator_pid, RootProposalBallot::Yes));
        } else {
            node_operator_ballots.push((node_operator_pid, RootProposalBallot::Undecided));
        }
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L363-370)
```rust
        // Add the ballots for this node operator.
        let mut voted_on: i32 = 0;
        for (p, b) in &mut proposal.node_operator_ballots {
            if p == &caller {
                *b = ballot.clone();
                voted_on += 1;
            }
        }
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L402-421)
```rust
    if proposal.is_byzantine_majority_yes() {
        println!(
            "{LOG_PREFIX}Root proposal from {proposer} to upgrade the governance canister to sha: {wasm_sha256:?} \
             was accepted. Votes: {votes_yes} Yes, {votes_no} No, {votes_undecided} Undecided. Upgrading."
        );
        let payload = proposal.payload.clone();
        PROPOSALS.with(|proposals| proposals.borrow_mut().remove(&proposer));
        // Check that the wasm of the governance canister is still the same.

        let current_governance_wasm_sha = get_current_governance_canister_wasm().await;
        if current_governance_wasm_sha != proposal.current_wasm_sha {
            let message = format!(
                "{}Invalid proposal. Expected governance wasm sha must match \
             the currently running governance wasm's sha. Current: {:?}. Expected: {:?}",
                LOG_PREFIX, current_governance_wasm_sha, proposal.current_wasm_sha
            );
            println!("{message}");
            return Err(message);
        }
        let _ = change_canister(payload).await;
```

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L113-126)
```rust
#[update(hidden = true)]
async fn vote_on_root_proposal_to_upgrade_governance_canister(
    proposer: PrincipalId,
    wasm_sha256: serde_bytes::ByteBuf,
    ballot: RootProposalBallot,
) -> Result<(), String> {
    ic_nns_handler_root::root_proposals::vote_on_root_proposal_to_upgrade_governance_canister(
        caller(),
        proposer,
        wasm_sha256.to_vec(),
        ballot,
    )
    .await
}
```
