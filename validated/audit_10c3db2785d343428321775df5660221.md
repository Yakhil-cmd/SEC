The code is concrete and readable. Let me verify the exact lines in question.

The critical section is at `rs/nns/handlers/root/impl/src/root_proposals.rs`, lines 363–370:

```rust
// Add the ballots for this node operator.
let mut voted_on: i32 = 0;
for (p, b) in &mut proposal.node_operator_ballots {
    if p == &caller {
        *b = ballot.clone();   // <-- unconditional overwrite, no prior-ballot check
        voted_on += 1;
    }
}
```

There is **no guard** checking whether `b` is already `Yes` or `No` before overwriting. The only checks performed before reaching this line are:

1. Proposal exists and is not expired [1](#0-0) 
2. Registry version unchanged [2](#0-1) 
3. Wasm SHA matches [3](#0-2) 
4. Caller is in the ballot list [4](#0-3) 

None of these checks prevent re-voting. The ballot is mutable and unconditionally overwritten. [5](#0-4) 

---

### Title
Ballot mutability in `vote_on_root_proposal_to_upgrade_governance_canister` allows a single NNS node operator to flip votes and block or force governance-canister upgrades — (`rs/nns/handlers/root/impl/src/root_proposals.rs`)

### Summary
The `vote_on_root_proposal_to_upgrade_governance_canister` function unconditionally overwrites a node operator's existing ballot with a new one. There is no immutability check on already-cast `Yes` or `No` ballots. A single registered NNS node operator — a role explicitly within the Byzantine fault tolerance budget of f = (N−1)/3 — can flip their vote arbitrarily many times, either permanently blocking a legitimate governance upgrade or forcing one through with fewer genuine Yes votes than the threshold requires.

### Finding Description
`GovernanceUpgradeRootProposal` stores ballots as a `Vec<(PrincipalId, RootProposalBallot)>` initialized with one entry per NNS node. [6](#0-5) 

When `vote_on_root_proposal_to_upgrade_governance_canister` is called, it iterates over `node_operator_ballots` and for every entry matching the caller's principal, executes `*b = ballot.clone()` without checking the prior value of `b`. [7](#0-6) 

The threshold check `is_byzantine_majority_yes` requires `votes_yes >= num_nodes - max_faults`. [8](#0-7) 

Because the ballot can be overwritten at any time before the threshold is reached, the tally is not monotone.

**Attack path 1 — persistent blocking (Yes → No):**
1. Attacker (a registered NNS node operator) calls `submit_root_proposal_to_upgrade_governance_canister`; their ballot is automatically set to `Yes`. [9](#0-8) 
2. Other operators vote `Yes` until the tally is `N − f − 1` (one short of threshold).
3. Attacker calls `vote_on_root_proposal_to_upgrade_governance_canister` with `ballot = No`; their `Yes` is overwritten with `No`, dropping the tally to `N − f − 2`.
4. If another operator votes `Yes` to restore the tally, the attacker flips again. This can be repeated indefinitely within the 7-day window.

**Attack path 2 — forced execution (No → Yes):**
1. A non-proposer node operator votes `No`.
2. Other operators vote `Yes` until the tally is `N − f − 1`.
3. Attacker flips their `No` to `Yes`, pushing the tally to `N − f` and triggering execution — with only `N − f − 1` genuine Yes votes.

### Impact Explanation
The governance canister controls all NNS canisters and all ICP/Cycles flows. A single malicious node operator — well within the f-fault budget the system is designed to tolerate — can:
- Permanently block any governance-canister upgrade for its full 7-day lifetime.
- Force an upgrade through with fewer genuine Yes votes than the Byzantine threshold requires, undermining the security guarantee of the root proposal mechanism.

### Likelihood Explanation
Any registered NNS node operator can exploit this. No key compromise, social engineering, or majority collusion is required. The call is a standard ingress message to the NNS root canister. The only precondition is that a proposal is pending and has not yet reached majority — a normal operational state.

### Recommendation
Add an immutability check before overwriting the ballot. Only allow a transition from `Undecided`:

```rust
for (p, b) in &mut proposal.node_operator_ballots {
    if p == &caller {
        if !matches!(b, RootProposalBallot::Undecided) {
            return Err(format!(
                "{LOG_PREFIX}Caller: {caller} has already cast a ballot."
            ));
        }
        *b = ballot.clone();
        voted_on += 1;
    }
}
```

This enforces the invariant that each node operator's ballot is immutable once cast, consistent with the Byzantine fault tolerance model the proposal mechanism is built on.

### Proof of Concept
State-machine test with a 7-node NNS subnet (N=7, f=2, threshold=5):

1. Operator A submits proposal → tally: 1 Yes.
2. Operators B, C, D vote Yes → tally: 4 Yes (one short of threshold=5).
3. Operator A calls `vote_on_root_proposal_to_upgrade_governance_canister(ballot=No)` → tally: 3 Yes, 1 No.
4. Assert proposal is not executed and tally is 3 Yes.
5. Operator E votes Yes → tally: 4 Yes again.
6. Operator A flips back to No → tally: 3 Yes again.
7. Assert proposal is permanently blocked by a single operator.

For the forced-execution variant: after step 2, have operator A (who voted No in a separate scenario) flip to Yes, pushing tally to 5 and triggering execution with only 4 genuine Yes votes.

### Citations

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L101-101)
```rust
    pub node_operator_ballots: Vec<(PrincipalId, RootProposalBallot)>,
```

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

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L234-236)
```rust
        if node_operator_pid == caller {
            voted_on += 1;
            node_operator_ballots.push((node_operator_pid, RootProposalBallot::Yes));
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L329-341)
```rust
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
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L343-352)
```rust
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
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L354-361)
```rust
        if wasm_sha256 != proposal.proposed_wasm_sha {
            let message = format!(
                "{}The sha of the wasm in the governance upgrade proposal that the voter intends to vote on: {:?}\
                 is not the same as the sha of the wasm: {:?} proposed by: {}", LOG_PREFIX, wasm_sha256,
                proposal.proposed_wasm_sha, proposer);
            println!("{message}");
            return Err(message);
        }
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L364-369)
```rust
        let mut voted_on: i32 = 0;
        for (p, b) in &mut proposal.node_operator_ballots {
            if p == &caller {
                *b = ballot.clone();
                voted_on += 1;
            }
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L372-378)
```rust
        if voted_on == 0 {
            let message = format!(
                "{LOG_PREFIX}Caller: {caller} is not eligible to vote on root proposal.",
            );
            println!("{message}");
            return Err(message);
        }
```
