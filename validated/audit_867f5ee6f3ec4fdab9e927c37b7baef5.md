### Title
Competing Root Proposals Allow a Swing Node Operator to Front-Run NNS Governance Canister Upgrade — (File: rs/nns/handlers/root/impl/src/root_proposals.rs)

---

### Summary

The NNS root canister's `vote_on_root_proposal_to_upgrade_governance_canister` function places no restriction on a node operator voting **Yes** on multiple simultaneously-pending competing proposals. Because proposals are stored per-proposer in a flat map, two proposals targeting different governance WASM binaries can coexist. A node operator who has not yet voted on either proposal can act as a "swing voter" and unilaterally determine which governance upgrade is executed, creating a front-running opportunity analogous to the oracle report-beacon race described in the external report.

---

### Finding Description

`GovernanceUpgradeRootProposal` entries are stored in a `thread_local` `BTreeMap<PrincipalId, GovernanceUpgradeRootProposal>` keyed by the **proposer's** principal, so any number of node operators may each hold one active proposal simultaneously. [1](#0-0) 

The passing threshold is `T = N − f`, where `f = (N−1)/3` (integer division). [2](#0-1) 

When a node operator casts a ballot, the code iterates over the ballots of the **single proposal** identified by `proposer` and updates the caller's entry. There is no global registry of which proposals a caller has already voted on, and no check that prevents the same caller from voting **Yes** on a second, competing proposal in a separate call. [3](#0-2) 

**Concrete scenario for N = 13 (T = 9):**

| Group | Size | Voted Yes on P_A | Voted Yes on P_B |
|---|---|---|---|
| D-group | 4 | ✓ | — |
| E-group | 4 | — | ✓ |
| C-group | 4 | ✓ | ✓ |
| Swing (F) | 1 | — | — |

After the D-, E-, and C-groups vote, both P_A and P_B sit at **8 / 9** yes-votes. Node operator F has not voted on either. Whichever proposal F votes on first crosses the threshold and triggers `change_canister`. [4](#0-3) 

---

### Impact Explanation

The governance canister controls the entire NNS: it manages subnets, node providers, and protocol upgrades. A successful root-proposal execution installs an arbitrary WASM binary as the governance canister via `change_canister`. If one of the two competing proposals carries a backdoored or malicious WASM, the swing voter — a single node operator below the Byzantine fault threshold — can unilaterally install it by racing to cast the deciding vote before any honest voter does. This constitutes a **governance authorization bug** with potential for full NNS compromise.

---

### Likelihood Explanation

- NNS subnet node operators are reachable protocol peers; each is individually below the fault threshold `f`.
- Competing proposals arise naturally whenever node operators disagree on which WASM to install (e.g., a hotfix race or a social-engineering campaign that convinces a subset of operators to submit a different binary).
- The C-group (operators who vote Yes on both proposals) need only be 4 out of 13 — a realistic coordination size.
- The swing voter (F) need only be one operator who has not yet voted; this is the normal state during any ongoing vote.
- No special privileges beyond being a registered NNS-subnet node operator are required to trigger the race.

---

### Recommendation

1. **Track cross-proposal votes**: Maintain a global set of `(caller, vote=Yes)` entries. Reject a Yes vote on proposal P_B from any caller who has already cast a Yes vote on any other active proposal. This mirrors the oracle recommendation of enforcing `Q > C/2` by ensuring the same voter cannot simultaneously contribute to two competing quorums.

2. **Alternatively, enforce single-active-proposal semantics**: When a node operator casts a Yes vote on proposal P_A, automatically record an abstain/No on all other active proposals for that operator, preventing them from being the deciding vote on a competing proposal.

3. **Add an invariant check**: After every vote, assert that no two active proposals simultaneously hold `votes_yes >= T − 1`. If this invariant is violated, log a warning so operators can investigate collusion.

---

### Proof of Concept

```
// Setup: NNS subnet has 13 nodes; threshold T = 9.
// Node operators: A, B, C1–C4, D1–D4, E1–E4, F

// Step 1: Two competing proposals are submitted.
A  → submit_root_proposal_to_upgrade_governance_canister(WASM_A)
B  → submit_root_proposal_to_upgrade_governance_canister(WASM_B)

// Step 2: D-group votes Yes only on P_A  (P_A: 5, P_B: 1)
D1–D4 → vote_on_root_proposal(proposer=A, ballot=Yes)

// Step 3: E-group votes Yes only on P_B  (P_A: 5, P_B: 5)
E1–E4 → vote_on_root_proposal(proposer=B, ballot=Yes)

// Step 4: C-group votes Yes on BOTH proposals  (P_A: 9? No — 5+4=9... wait)
```

Corrected count for clarity (A and B each auto-vote Yes on their own proposal):

```
After Step 2: P_A yes=5 (A + D1–D4), P_B yes=1 (B)
After Step 3: P_A yes=5,             P_B yes=5 (B + E1–E4)
Step 4: C1–C4 vote Yes on P_A → P_A yes=9 ... 
```

To keep both at 8, adjust group sizes: use D-group=3, E-group=3, C-group=4, swing=F (total=13 including A and B as proposers who auto-vote):

```
P_A yes = A(1) + D1–D3(3) + C1–C4(4) = 8  ← one short
P_B yes = B(1) + E1–E3(3) + C1–C4(4) = 8  ← one short

// Step 5: F is the swing voter.
F → vote_on_root_proposal(proposer=A, ballot=Yes)
// P_A reaches 9/9 → change_canister(WASM_A) executes immediately.
// P_B is abandoned at 8/9.
```

Node operator F — a single protocol peer below the fault threshold — unilaterally determined which governance binary was installed on the NNS. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L110-122)
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

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L142-144)
```rust
thread_local! {
  static PROPOSALS: RefCell<BTreeMap<PrincipalId, GovernanceUpgradeRootProposal>> = const { RefCell::new(BTreeMap::new()) };
}
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L175-286)
```rust
pub async fn submit_root_proposal_to_upgrade_governance_canister(
    caller: PrincipalId,
    expected_governance_wasm_sha: Vec<u8>,
    request: ChangeCanisterRequest,
) -> Result<(), String> {
    let now = now_seconds();

    // This is a new proposal and we're ready to prepare it.
    // Do some simple validation first:
    // - That the wasm has some bytes in it.
    // - That it targets the governance canister.
    // - That it is an upgrade (reinstall is not supported).
    if request.wasm_module.is_empty()
        || request.canister_id != GOVERNANCE_CANISTER_ID
        || request.mode != CanisterInstallMode::Upgrade
    {
        let message = format!(
            "{LOG_PREFIX}Invalid proposal. Proposal must be an upgrade proposal \
             to the governance canister with some wasm."
        );
        println!("{message}");
        return Err(message);
    }

    // Get the sha256 of the currently installed governance canister and
    // make sure it matches the one on the proposal (we'll check it again
    // on execution, but we check it here first to provide a nice error
    // message to the user).
    let current_governance_wasm_sha = get_current_governance_canister_wasm().await;
    if expected_governance_wasm_sha != current_governance_wasm_sha {
        let message = format!(
            "{LOG_PREFIX}Invalid proposal. Expected governance wasm sha must match \
             the currently running governance wasm's sha. Current: {current_governance_wasm_sha:?}. Expected: {expected_governance_wasm_sha:?}"
        );
        println!("{message}");
        return Err(message);
    }

    // Get the node operators of the nns subnet from the registry and how
    // many nodes each of them controls. In order to do this we need to:
    // - Get the principal id of the nns subnet
    // - Get the list of nodes
    // - Get the node operators, for each node.
    let mut node_operator_ballots = Vec::new();
    let nns_subnet_id = get_nns_subnet_id()
        .await
        .map_err(|e| format!("Error: {e:?}"))?;
    let (nns_nodes, subnet_membership_registry_version) = get_nns_membership(&nns_subnet_id)
        .await
        .map_err(|e| format!("Error: {e:?}"))?;

    let mut voted_on: i32 = 0;
    let mut total_votes: i32 = 0;
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
    }

    // Check if the caller is among those principals, if it is it will have
    // cast at least one ballot.
    if voted_on == 0 {
        let message = format!(
            "{LOG_PREFIX}Invalid proposal. Caller: {caller} must be among the node operators of the nns subnet."
        );
        println!("{message}");
        return Err(message);
    }

    PROPOSALS.with(|proposals| {
        // Check whether there is a previous proposal from the same principal and log
        // that we'll be replacing it.
        if let Some(previous_proposal_from_the_same_principal) = proposals.borrow().get(&caller) {
            println!(
                "{LOG_PREFIX}Current root proposal {previous_proposal_from_the_same_principal:?} from {caller} is going to be overwritten.",
            );
        }

        // Store the proposal, the current list of principals that can vote,
        // together with the version number and as many votes for 'yes' as the
        // number of nodes the caller's principal operates, in the nns subnetwork.
        let proposed_wasm_sha = ic_crypto_sha2::Sha256::hash(&request.wasm_module).to_vec();

        proposals.borrow_mut().insert(
            caller,
            GovernanceUpgradeRootProposal {
                nns_subnet_id,
                current_wasm_sha: current_governance_wasm_sha.clone(),
                proposed_wasm_sha: proposed_wasm_sha.clone(),
                payload: request,
                proposer: caller,
                node_operator_ballots,
                subnet_membership_registry_version,
                submission_timestamp_seconds: now,
            },
        );

        println!(
            "{LOG_PREFIX}Root proposal to upgrade the governance canister from: {current_governance_wasm_sha:?} to {proposed_wasm_sha:?}, \
             proposed by: {caller:?} was submitted. Current tally: {voted_on}/{total_votes}"
        );
    });
    Ok(())
}
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L303-434)
```rust
pub async fn vote_on_root_proposal_to_upgrade_governance_canister(
    caller: PrincipalId,
    proposer: PrincipalId,
    wasm_sha256: Vec<u8>,
    ballot: RootProposalBallot,
) -> Result<(), String> {
    let proposal = get_proposal_clone(&proposer)?;

    let (_, version) = get_nns_membership(&proposal.nns_subnet_id)
        .await
        .map_err(|e| format!("Error executing proposal: {e:?}"))?;

    // Check all the constraints and vote (without any async calls in between).
    PROPOSALS.with(|proposals| {
        let mut proposals = proposals.borrow_mut();
        let proposal = proposals.get_mut(&proposer);
        if proposal.is_none() {
            let message = format!(
                "No root governance upgrade proposal from {proposer} is pending"
            );
            println!("{message}");
            return Err(message);
        }
        let proposal = proposal.unwrap();
        let now = now_seconds();

        // Check the submission time, if it has elapsed without a majority
        // we can delete it.
        if now
            > (proposal.submission_timestamp_seconds + MAX_TIME_FOR_GOVERNANCE_UPGRADE_ROOT_PROPOSAL)
        {
            proposals.remove(&proposer);
            let message = format!(
                "{LOG_PREFIX}Current root governance upgrade proposal from {proposer} is too old.\
                 Deleting.",
            );
            println!("{message}");
            return Err(message);
        }

        // Check that the version of the record on the registry is still the same.
        if version != proposal.subnet_membership_registry_version {
            proposals.remove(&proposer);
            let message = format!(
                "{LOG_PREFIX}Registry version of the subnet record changed since the \
                 proposal from {proposer} was submitted. Deleting.",
            );
            println!("{message}");
            return Err(message);
        }

        if wasm_sha256 != proposal.proposed_wasm_sha {
            let message = format!(
                "{}The sha of the wasm in the governance upgrade proposal that the voter intends to vote on: {:?}\
                 is not the same as the sha of the wasm: {:?} proposed by: {}", LOG_PREFIX, wasm_sha256,
                proposal.proposed_wasm_sha, proposer);
            println!("{message}");
            return Err(message);
        }

        // Add the ballots for this node operator.
        let mut voted_on: i32 = 0;
        for (p, b) in &mut proposal.node_operator_ballots {
            if p == &caller {
                *b = ballot.clone();
                voted_on += 1;
            }
        }

        if voted_on == 0 {
            let message = format!(
                "{LOG_PREFIX}Caller: {caller} is not eligible to vote on root proposal.",
            );
            println!("{message}");
            return Err(message);
        }
        Ok(())
    })?;

    // Get the proposal once more. Once the proposal is accepted or rejected it's
    // state is final, so it's ok to clone and execute from the clone.
    let proposal = get_proposal_clone(&proposer)?;

    let mut votes_yes: i32 = 0;
    let mut votes_no: i32 = 0;
    let mut votes_undecided: i32 = 0;
    for (_, b) in &proposal.node_operator_ballots {
        match b {
            RootProposalBallot::Yes => votes_yes += 1,
            RootProposalBallot::No => votes_no += 1,
            RootProposalBallot::Undecided => votes_undecided += 1,
        }
    }

    println!(
        "{LOG_PREFIX}Vote(s) on root proposal to upgrade the governance canister to sha {wasm_sha256:?} \
         from: {proposer:?} were accepted. Current tally: {votes_yes} Yes, {votes_no} No, {votes_undecided} Undecided."
    );

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
        Ok(())
    } else if proposal.is_byzantine_majority_no() {
        PROPOSALS.with(|proposals| proposals.borrow_mut().remove(&proposer));
        let message = format!(
            "{LOG_PREFIX}Root proposal from {proposer} to upgrade the governance canister to sha: {wasm_sha256:?} \
             was rejected. Votes: {votes_yes} Yes, {votes_no} No, {votes_undecided} Undecided. Deleting."
        );
        println!("{message}");
        Ok(())
    } else {
        Ok(())
    }
}
```
