### Title
Root Proposal Proposer Can Re-Vote on Their Own Proposal - (File: `rs/nns/handlers/root/impl/src/root_proposals.rs`)

### Summary
`vote_on_root_proposal_to_upgrade_governance_canister` does not check whether `caller == proposer`. A node operator who submitted a root proposal can call the vote function on their own proposal to overwrite their automatically-cast `Yes` ballot with `No` or `Undecided`, and then flip it back to `Yes`. This mirrors the external report's pattern: the submitter/funder role and the approver role are held by the same principal with no guard.

### Finding Description
In `submit_root_proposal_to_upgrade_governance_canister`, when the proposer's principal matches a node they operate, their ballot is immediately set to `RootProposalBallot::Yes`:

```rust
if node_operator_pid == caller {
    voted_on += 1;
    node_operator_ballots.push((node_operator_pid, RootProposalBallot::Yes));
}
``` [1](#0-0) 

The proposal is then stored with the proposer's principal as the map key and their ballots pre-set to `Yes`. [2](#0-1) 

In `vote_on_root_proposal_to_upgrade_governance_canister`, the only eligibility check is whether `caller` appears in `node_operator_ballots`. There is **no check that `caller != proposer`**:

```rust
for (p, b) in &mut proposal.node_operator_ballots {
    if p == &caller {
        *b = ballot.clone();
        voted_on += 1;
    }
}
``` [3](#0-2) 

Because the proposer's principal is always present in `node_operator_ballots`, the proposer can call `vote_on_root_proposal_to_upgrade_governance_canister` on their own proposal and overwrite their ballot with any value (`Yes`, `No`, or `Undecided`).

The NNS/SNS governance canisters handle this correctly by recording the proposer's `Yes` vote at proposal creation time and then rejecting any subsequent explicit vote with `NeuronAlreadyVoted`. The root proposal system has no equivalent guard. [4](#0-3) 

### Impact Explanation
A node operator who submitted a root proposal can:

1. **Suppress their own votes**: Call `vote_on_root_proposal_to_upgrade_governance_canister` with `ballot = No` or `Undecided` immediately after submission, reducing the `Yes` count and potentially keeping the proposal below the Byzantine-majority threshold (`N - f` votes) indefinitely.
2. **Flip votes strategically**: After other operators have voted `Yes` and the tally is near the threshold, the proposer can flip their ballots to `No` to block execution, then re-flip to `Yes` to allow it — effectively controlling the exact moment the proposal crosses the threshold.
3. **Invalidate a near-passing proposal**: If the proposer controls enough nodes to be the swing votes, they can prevent a proposal they themselves submitted from executing, forcing a re-submission cycle.

The `is_byzantine_majority_yes` check requires `votes_yes >= (num_nodes - max_faults)`. [5](#0-4) 

A proposer controlling multiple NNS nodes can shift the tally across this threshold at will.

### Likelihood Explanation
The entry path is a valid ingress call to the root canister's `vote_on_root_proposal_to_upgrade_governance_canister` endpoint by any NNS node operator — the same class of principal already permitted to submit proposals. No additional privilege escalation is required. The scenario where a proposer wants to block their own proposal is unusual but not impossible (e.g., changed intent, social-engineering reversal, or deliberate griefing of the upgrade process). The missing check is a straightforward omission with a clear code path.

### Recommendation
Add an explicit guard at the top of `vote_on_root_proposal_to_upgrade_governance_canister` before any state is read or mutated:

```rust
if caller == proposer {
    let message = format!(
        "{LOG_PREFIX}Proposer {proposer} cannot vote on their own proposal."
    );
    println!("{message}");
    return Err(message);
}
``` [6](#0-5) 

This mirrors the pattern used in NNS/SNS governance where the proposer's ballot is locked at submission time and cannot be re-cast.

### Proof of Concept

1. Node operator **A** (controlling 3 of 7 NNS nodes) calls `submit_root_proposal_to_upgrade_governance_canister`. Their 3 ballots are set to `Yes` automatically. Tally: 3 Yes / 0 No / 4 Undecided. Threshold: 5 Yes needed.
2. Operators B and C each vote `Yes`. Tally: 5 Yes / 0 No / 2 Undecided — threshold reached.
3. Before `vote_on_root_proposal_to_upgrade_governance_canister` triggers execution, operator **A** calls `vote_on_root_proposal_to_upgrade_governance_canister(proposer=A, ballot=No)`. Their 3 ballots flip to `No`. Tally: 2 Yes / 3 No / 2 Undecided — below threshold.
4. `is_byzantine_majority_yes()` returns `false`; the proposal does not execute.
5. Operator A can flip back to `Yes` at any chosen moment, controlling execution timing unilaterally. [7](#0-6)

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

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L234-239)
```rust
        if node_operator_pid == caller {
            voted_on += 1;
            node_operator_ballots.push((node_operator_pid, RootProposalBallot::Yes));
        } else {
            node_operator_ballots.push((node_operator_pid, RootProposalBallot::Undecided));
        }
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L266-278)
```rust
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
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L303-380)
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
```

**File:** rs/nns/governance/src/governance.rs (L5642-5648)
```rust
        if neuron_ballot.vote != (Vote::Unspecified as i32) {
            // Already voted.
            return Err(GovernanceError::new_with_message(
                ErrorType::NeuronAlreadyVoted,
                "Neuron already voted on proposal.",
            ));
        }
```
