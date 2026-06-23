### Title
Root Proposal Vote Invalidation via Same-Principal Proposal Overwrite Denial of Service - (File: rs/nns/handlers/root/impl/src/root_proposals.rs)

### Summary

The NNS Root canister's `submit_root_proposal_to_upgrade_governance_canister` function explicitly allows any NNS node operator to overwrite their own pending root proposal at any time, resetting all accumulated votes. Because the root proposal system requires a Byzantine majority (N - f votes) to execute, a malicious node operator who has already cast a "yes" vote on their own proposal can repeatedly resubmit a new proposal to reset the vote tally, indefinitely blocking execution of any governance upgrade that depends on their participation.

### Finding Description

The `PROPOSALS` map in `rs/nns/handlers/root/impl/src/root_proposals.rs` stores at most one pending root proposal per `PrincipalId` (node operator). When `submit_root_proposal_to_upgrade_governance_canister` is called by a principal that already has a pending proposal, the old proposal — including all accumulated votes — is silently overwritten:

```rust
// Line 252-278
PROPOSALS.with(|proposals| {
    // Check whether there is a previous proposal from the same principal and log
    // that we'll be replacing it.
    if let Some(previous_proposal_from_the_same_principal) = proposals.borrow().get(&caller) {
        println!(
            "{LOG_PREFIX}Current root proposal ... from {caller} is going to be overwritten.",
        );
    }
    proposals.borrow_mut().insert(
        caller,
        GovernanceUpgradeRootProposal { ... },
    );
});
```

The Byzantine majority threshold is `N - f` where `f = (N-1)/3`. For a 7-node NNS subnet, this requires 5 yes votes. The attacker's own proposal starts with their own yes vote(s) already counted. Other node operators then vote yes. At any point before the threshold is reached, the attacker can call `submit_root_proposal_to_upgrade_governance_canister` again with a different (or identical) WASM, resetting the vote tally to only their own initial yes vote(s). This can be repeated indefinitely.

The entry path is fully open to any NNS node operator via the `submit_root_proposal_to_upgrade_governance_canister` update call on the Root canister, which is exposed without rate limiting or cooldown. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

A single malicious NNS node operator can permanently block any root governance upgrade proposal from reaching the execution threshold. Since the root proposal mechanism is the only path to upgrade the NNS Governance canister without going through Governance itself (i.e., the emergency upgrade path), blocking it constitutes a denial of service against the IC's emergency governance upgrade mechanism. The attacker does not need to corrupt any cryptographic primitive or hold a majority — a single node operator suffices. The impact is permanent unavailability of the root proposal upgrade path for as long as the attacker continues to resubmit. [4](#0-3) 

### Likelihood Explanation

The attacker must be a legitimate NNS node operator (i.e., their principal must correspond to a node on the NNS subnet). This is a restricted but non-trivial set of actors. The attack requires no special tooling beyond calling the public `submit_root_proposal_to_upgrade_governance_canister` update method repeatedly. The attack is only relevant when a root proposal is actually needed (i.e., during an emergency governance upgrade), which is a rare but high-stakes scenario. Likelihood is low in normal operation but becomes relevant precisely when the mechanism is most needed. [5](#0-4) [6](#0-5) 

### Recommendation

1. **Prevent overwriting a proposal that has already received votes from other operators.** Before allowing a resubmission, check whether any other node operator has already cast a ballot on the existing proposal. If so, reject the resubmission.
2. **Alternatively, add a cooldown period** between successive submissions from the same principal, preventing rapid resubmission.
3. **Emit an on-chain event or log** when a proposal with accumulated votes is overwritten, so that other node operators are alerted.

### Proof of Concept

1. Node operator A (controlling 1 node on a 7-node NNS subnet) calls `submit_root_proposal_to_upgrade_governance_canister` with WASM_v1. Their proposal is stored with 1 yes vote (their own).
2. Node operators B, C, D each call `vote_on_root_proposal_to_upgrade_governance_canister` with yes. The tally is now 4/7 (threshold is 5).
3. Before operator E votes, operator A calls `submit_root_proposal_to_upgrade_governance_canister` again with WASM_v2 (or even the same WASM). The old proposal with 4 votes is silently deleted and replaced with a new proposal having only 1 vote.
4. Operators B, C, D must vote again. Operator A repeats step 3 each time the tally approaches 5.
5. The governance upgrade is permanently blocked as long as operator A continues resubmitting. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L106-122)
```rust
impl GovernanceUpgradeRootProposal {
    /// For a root proposal to have a byzantine majority of yes, it
    /// needs to collect N - f ""yes"" votes, where N is the total number
    /// of nodes (same as the number of ballots) and f = (N - 1) / 3.
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

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L161-174)
```rust
/// Submits a "root" governance upgrade proposal.
///
/// The caller must be the principal corresponding to a node operator currently
/// running a node on the nns subnetwork.
///
/// These situations will delete a root proposal:
/// - There can be only one "root" proposal pending from a given principal at a
///   time, if there is already a proposal pending from the same principal the
///   old proposal is deleted and replaced with the new one, voting is reset.
/// - Root proposals are only available for voting for 7 days. After this period
///   the proposal can't be accepted and is deleted, upon receiving a vote or a
///   get request or the submission of a new one.
/// - Root proposals are not stored in stable storage, an upgrade of the root
///   canister will delete the currently pending root proposal, if there is one.
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L175-211)
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
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L252-285)
```rust
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
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L363-379)
```rust
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
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L402-422)
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
        Ok(())
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
