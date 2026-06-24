### Title
Node Operator Can Vote Multiple Times with Different Outcomes on Root Governance Upgrade Proposals - (`File: rs/nns/handlers/root/impl/src/root_proposals.rs`)

### Summary

`vote_on_root_proposal_to_upgrade_governance_canister` in the NNS root canister unconditionally overwrites a node operator's ballot on every call, with no check for whether the caller has already voted. A single NNS subnet node operator (a protocol peer well below the Byzantine fault threshold) can flip their vote from `Yes` to `No` or `No` to `Yes` an unlimited number of times, undermining the integrity of the root proposal voting mechanism.

### Finding Description

The root proposal mechanism collects votes from NNS subnet node operators to upgrade the governance canister. It is designed to tolerate up to `f = (N-1)/3` Byzantine nodes. The voting function `vote_on_root_proposal_to_upgrade_governance_canister` performs several validity checks (proposal existence, timeout, registry version, wasm SHA), but the ballot-writing loop at lines 363–370 contains no guard against re-voting:

```rust
// Add the ballots for this node operator.
let mut voted_on: i32 = 0;
for (p, b) in &mut proposal.node_operator_ballots {
    if p == &caller {
        *b = ballot.clone();   // unconditional overwrite — no prior-vote check
        voted_on += 1;
    }
}
```

The `RootProposalBallot` enum has three states (`Yes`, `No`, `Undecided`), but the code never checks whether `b` is already `Yes` or `No` before overwriting it. Any eligible node operator can call the update endpoint repeatedly with alternating ballot values. [1](#0-0) 

The public canister entry point is: [2](#0-1) 

By contrast, the NNS and SNS governance `register_vote` functions both explicitly reject re-votes: [3](#0-2) [4](#0-3) 

The root proposal voting function has no equivalent guard.

### Impact Explanation

For a 7-node NNS subnet, `f = 2`, so 5 Yes votes are needed to pass and 3 No votes to reject. A single node operator controlling 1 node can:

1. **Block a legitimate upgrade indefinitely**: Vote `Yes` when 4 other nodes have voted `Yes` (reaching the 5-vote threshold would execute the proposal), then immediately flip to `No` before the threshold is reached — or strategically flip back and forth to keep the tally below the threshold until the 7-day timeout expires.
2. **Prevent legitimate rejection**: Vote `No` when 2 other nodes have voted `No` (one vote away from the 3-vote rejection threshold), then flip to `Yes` to remove their `No` vote and prevent the proposal from being rejected.
3. **Amplify impact with multi-node operators**: A node operator controlling multiple nodes on the NNS subnet has proportionally more ballot entries (one per node they operate), and can flip all of them simultaneously, giving them outsized influence over the tally.

The root proposal mechanism is the last-resort path to upgrade the governance canister when normal NNS governance is broken. Disrupting it has high-severity consequences for the entire IC governance stack.

### Likelihood Explanation

The attack requires only that the caller be a registered NNS subnet node operator — a role held by a small number of entities (~13 on the NNS subnet). The call is a standard ingress update to the root canister with no additional authentication beyond the caller's principal. No privileged key, governance majority, or subnet-majority corruption is required. A single below-threshold node operator can execute this attack unilaterally.

### Recommendation

Before overwriting the ballot, check whether the caller has already cast a non-`Undecided` vote and reject the call if so:

```rust
for (p, b) in &mut proposal.node_operator_ballots {
    if p == &caller {
        if !matches!(b, RootProposalBallot::Undecided) {
            return Err(format!("Caller {caller} has already voted."));
        }
        *b = ballot.clone();
        voted_on += 1;
    }
}
```

This mirrors the protection already present in NNS and SNS governance `register_vote`.

### Proof of Concept

1. Node operator `A` controls 1 node on a 7-node NNS subnet. A root proposal is submitted by another operator.
2. Nodes B, C, D, E each vote `Yes` (4 Yes, threshold = 5).
3. `A` calls `vote_on_root_proposal_to_upgrade_governance_canister` with `ballot = Yes` → tally becomes 5 Yes → **proposal executes immediately** (this is the intended path).
4. **Attack variant**: Before step 3, `A` calls with `ballot = No` (tally: 4 Yes, 1 No). Then `A` calls again with `ballot = Yes` (tally: 5 Yes, 0 No) — demonstrating the flip is accepted. `A` can repeat this indefinitely to keep the tally oscillating below the threshold, preventing execution until the 7-day timeout.
5. **Rejection-blocking variant**: With 2 No votes already cast (one away from the 3-vote rejection threshold), `A` votes `No` (3 No → proposal deleted). Then `A` re-submits a new proposal and votes `No` then flips to `Yes` to prevent the 3-No threshold from being reached by other voters. [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L106-140)
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

    /// For a root proposal to have a byzantine majority of no, it
    /// needs to collect f + 1 "no" votes, where N s the total number
    /// of nodes (same as the number of ballots) and f = (N - 1) / 3.
    fn is_byzantine_majority_no(&self) -> bool {
        let num_nodes = self.node_operator_ballots.len();
        let max_faults = (num_nodes - 1) / 3;
        let votes_no: usize = self
            .node_operator_ballots
            .iter()
            .map(|(_, b)| match b {
                RootProposalBallot::No => 1,
                _ => 0,
            })
            .sum();
        votes_no > max_faults
    }
}
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

**File:** rs/sns/governance/src/governance.rs (L3906-3912)
```rust
        if neuron_ballot.vote != (Vote::Unspecified as i32) {
            // Already voted.
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Neuron already voted on proposal.",
            ));
        }
```
