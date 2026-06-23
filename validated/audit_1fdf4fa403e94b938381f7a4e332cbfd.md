### Title
`vote_on_root_proposal_to_upgrade_governance_canister`: Node Operator Can Vote Multiple Times, Changing Ballot After It Has Been Cast - (`File: rs/nns/handlers/root/impl/src/root_proposals.rs`)

### Summary

The `vote_on_root_proposal_to_upgrade_governance_canister` function in the NNS root canister lacks a check that a node operator's ballot is still `Undecided` before overwriting it. Any eligible node operator can call the function repeatedly, changing their vote from `Yes` to `No` (or vice versa) after it has already been cast. This is a direct analog of H-08: the same missing "has-voted" guard that allowed repeated veto accumulation in the Solidity contract is absent here, allowing repeated ballot mutation in the IC root proposal mechanism.

### Finding Description

The `GovernanceUpgradeRootProposal` struct stores ballots as a `Vec<(PrincipalId, RootProposalBallot)>`, with one entry per node the operator controls on the NNS subnet. The three ballot states are `Yes`, `No`, and `Undecided`. [1](#0-0) 

When `vote_on_root_proposal_to_upgrade_governance_canister` is called, the voting loop unconditionally overwrites every ballot entry matching the caller's `PrincipalId`, regardless of whether that entry is already `Yes` or `No`: [2](#0-1) 

There is no guard equivalent to the NNS governance check:

```rust
if neuron_ballot.vote != (Vote::Unspecified as i32) {
    return Err(...NeuronAlreadyVoted...);
}
``` [3](#0-2) 

The SNS governance has the same guard: [4](#0-3) 

The root proposal mechanism has no such protection.

### Impact Explanation

A node operator who has already voted `Yes` can call `vote_on_root_proposal_to_upgrade_governance_canister` again with `No` (and vice versa), mutating their ballot after it was cast. Because `is_byzantine_majority_yes` and `is_byzantine_majority_no` count the current state of all ballots at the time of each call, a node operator can:

1. Cast a `Yes` vote to push the tally toward the acceptance threshold.
2. Immediately call again with `No` to retract that support and push toward rejection.
3. Repeat arbitrarily until the proposal expires or a majority is reached by other voters.

This undermines the integrity of the root proposal voting process. The root proposal is the only mechanism to upgrade the NNS governance canister outside of normal NNS governance, making its integrity critical. [5](#0-4) 

### Likelihood Explanation

The entry point is the publicly callable canister update method `vote_on_root_proposal_to_upgrade_governance_canister` on the NNS root canister. Any principal that is a node operator on the NNS subnet at the time a root proposal is submitted is eligible to call it. No privileged key or admin access beyond being a registered NNS node operator is required. The call can be made repeatedly with no rate limiting. [6](#0-5) 

### Recommendation

Before overwriting a ballot, check that it is still `Undecided`. Reject the call if the ballot has already been set to `Yes` or `No`:

```rust
// Add the ballots for this node operator.
let mut voted_on: i32 = 0;
for (p, b) in &mut proposal.node_operator_ballots {
    if p == &caller {
+       if !matches!(b, RootProposalBallot::Undecided) {
+           return Err(format!(
+               "{}Caller: {} has already voted on this root proposal.",
+               LOG_PREFIX, caller
+           ));
+       }
        *b = ballot.clone();
        voted_on += 1;
    }
}
```

This mirrors the `NeuronAlreadyVoted` guard used in both NNS and SNS governance. [7](#0-6) 

### Proof of Concept

Given a 7-node NNS subnet (f=2, threshold=5), where node operator `A` controls 1 node:

1. A root proposal is submitted by some proposer.
2. `A` calls `vote_on_root_proposal_to_upgrade_governance_canister` with `ballot = Yes`. Their entry becomes `Yes`. Tally: 1 Yes.
3. `A` calls again with `ballot = No`. Their entry becomes `No`. Tally: 0 Yes, 1 No.
4. `A` calls again with `ballot = Yes`. Their entry becomes `Yes` again.

Steps 2–4 succeed without error because the loop at lines 365–370 has no `Undecided` guard. A node operator controlling enough nodes could flip the tally across the acceptance or rejection threshold at will, as long as no majority has been reached yet. [8](#0-7)

### Citations

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L100-103)
```rust
    /// The ballots cast by node operators.
    pub node_operator_ballots: Vec<(PrincipalId, RootProposalBallot)>,
    /// The timestamp, in seconds, at which the proposal was submitted.
    pub submission_timestamp_seconds: u64,
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L106-139)
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
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L303-308)
```rust
pub async fn vote_on_root_proposal_to_upgrade_governance_canister(
    caller: PrincipalId,
    proposer: PrincipalId,
    wasm_sha256: Vec<u8>,
    ballot: RootProposalBallot,
) -> Result<(), String> {
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L363-380)
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
