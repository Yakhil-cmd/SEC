### Title
Malicious Root Proposal Proposer Can Prevent Governance Upgrade by Overwriting Pending Proposal - (File: rs/nns/handlers/root/impl/src/root_proposals.rs)

### Summary
A malicious or compromised NNS node operator who has submitted a root proposal to upgrade the governance canister can prevent the upgrade from executing by repeatedly overwriting their own pending proposal just before the Byzantine voting threshold is reached, resetting all accumulated votes. This is a direct analog to the Compound `_setPendingPauseGuardian` race: the current "pending" holder can overwrite the in-flight state to block the transition.

### Finding Description
The root proposal mechanism (`submit_root_proposal_to_upgrade_governance_canister` / `vote_on_root_proposal_to_upgrade_governance_canister`) is the **only** out-of-band path to upgrade a compromised governance canister without going through governance itself. Proposals are stored in a `thread_local` `BTreeMap<PrincipalId, GovernanceUpgradeRootProposal>` keyed by the proposer's principal ID. [1](#0-0) 

When a node operator calls `submit_root_proposal_to_upgrade_governance_canister` a second time, the existing proposal from that same principal is **silently overwritten** with a fresh one, resetting `node_operator_ballots` to all-`Undecided` (except the proposer's own automatic `Yes`): [2](#0-1) 

There is no guard that prevents overwriting once votes have been accumulated. The comment at line 166–169 even documents this as intentional behavior: [3](#0-2) 

**Attack sequence:**
1. Malicious/compromised node operator **A** calls `submit_root_proposal_to_upgrade_governance_canister` with a legitimate wasm payload.
2. Honest node operators B, C, D, E cast `Yes` ballots via `vote_on_root_proposal_to_upgrade_governance_canister`, accumulating votes toward the Byzantine threshold (N − f).
3. Just before the threshold is reached, **A** calls `submit_root_proposal_to_upgrade_governance_canister` again (same or different wasm). The `PROPOSALS` map entry for A is replaced; all previously cast ballots are gone.
4. Voters B, C, D, E must start over. A can repeat step 3 indefinitely.

A secondary TOCTOU window exists inside `vote_on_root_proposal_to_upgrade_governance_canister`: the function reads a proposal clone at line 309, then suspends at the `get_nns_membership` async call (line 311–313), then re-reads the live map at line 318. If A submits a new proposal with the **same** wasm sha during that suspension, the voter's ballot lands on the fresh (vote-reset) proposal rather than the one they inspected: [4](#0-3) 

### Impact Explanation
The root proposal mechanism is the sole emergency path to replace a compromised governance canister without governance participation. A single malicious node operator (well below the Byzantine fault threshold) can indefinitely delay or block the governance upgrade by repeatedly overwriting their own proposal. Because the mechanism is keyed per-proposer, the attacker does not need to interfere with other node operators' proposals — they only need to be the proposer that the honest majority is coordinating around (e.g., the first or most trusted operator to step forward in an emergency). Every reset forces the entire voting round to restart, burning the 7-day expiry window and requiring fresh coordination among all voters.

### Likelihood Explanation
The attack requires a node operator whose key is compromised or who acts maliciously. Node operators are permissioned participants, but key compromise is a realistic threat, especially during an emergency governance-recovery scenario (the exact scenario this mechanism is designed for). The attacker needs only to monitor the vote tally (via the public `get_pending_root_proposals_to_upgrade_governance_canister` endpoint) and submit a replacement proposal before the threshold is crossed. No special tooling beyond `ic-admin` is required. [5](#0-4) 

### Recommendation
1. **Prevent overwrite once votes are cast**: Before inserting a new proposal, check whether the existing proposal from the same principal has any non-`Undecided` ballots. If it does, reject the new submission (or require the proposer to explicitly withdraw first via a separate call).
2. **Alternatively, adopt a single-step admin-only pattern** (as Compound did): require a supermajority of node operators to co-sign the submission itself, eliminating the separate vote phase and the overwrite window entirely.
3. **At minimum**, emit a warning and require an explicit `force_overwrite` flag when overwriting a proposal that has accumulated votes, so the action is not silent.

### Proof of Concept
```
# Step 1: Attacker (node operator A) submits a legitimate proposal
ic-admin submit-root-proposal-to-upgrade-governance-canister \
  --wasm-module-path governance.wasm ...

# Step 2: Honest operators B, C, D, E vote Yes (tally: 4/15 needed)
ic-admin vote-on-root-proposal ... --ballot yes

# Step 3: Attacker monitors tally via public query
ic-admin get-pending-root-proposals-to-upgrade-governance-canister
# → sees tally approaching threshold

# Step 4: Attacker re-submits (overwrites) just before threshold
ic-admin submit-root-proposal-to-upgrade-governance-canister \
  --wasm-module-path governance_v2.wasm ...
# → node_operator_ballots reset; all 4 Yes votes are gone

# Step 5: Repeat indefinitely — governance upgrade is blocked
```

The overwrite is confirmed by the `PROPOSALS.borrow_mut().insert(caller, ...)` call at line 266, which unconditionally replaces any existing entry for the proposer principal with a freshly initialized ballot set. [6](#0-5)

### Citations

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L142-144)
```rust
thread_local! {
  static PROPOSALS: RefCell<BTreeMap<PrincipalId, GovernanceUpgradeRootProposal>> = const { RefCell::new(BTreeMap::new()) };
}
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L166-174)
```rust
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

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L252-284)
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
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L309-320)
```rust
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
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L446-462)
```rust
pub fn get_pending_root_proposals_to_upgrade_governance_canister()
-> Vec<GovernanceUpgradeRootProposal> {
    // Return the pending proposals, but strip the wasm so that the response stays
    // small.
    PROPOSALS.with(|proposals| {
        proposals
            .borrow()
            .values()
            .map(|p| {
                let mut p = p.clone();
                p.payload.wasm_module = vec![];
                p.payload.arg = vec![];
                p
            })
            .collect()
    })
}
```
