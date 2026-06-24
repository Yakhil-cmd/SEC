### Title
Malicious NNS Node Operator Can Indefinitely Prevent Root Proposal Rejection via Unbounded Vote Reset - (File: rs/nns/handlers/root/impl/src/root_proposals.rs)

---

### Summary

`submit_root_proposal_to_upgrade_governance_canister` in the NNS root canister allows any NNS subnet node operator to silently overwrite their own pending root proposal at any time with no cooldown or rate limit. Because resubmission resets all accumulated ballots, a malicious node operator can monitor the vote tally and resubmit just before the Byzantine rejection threshold (`f+1` "no" votes) is reached, keeping a malicious governance-upgrade proposal alive indefinitely.

---

### Finding Description

Root proposals to upgrade the NNS governance canister are stored in a `thread_local` `BTreeMap<PrincipalId, GovernanceUpgradeRootProposal>` keyed by the proposer's principal. [1](#0-0) 

When `submit_root_proposal_to_upgrade_governance_canister` is called by a node operator who already has a pending proposal, the function logs a warning and unconditionally replaces the old entry — including all ballots cast by other node operators — with a fresh proposal that carries only the proposer's own automatic "yes" votes: [2](#0-1) 

There is no cooldown period, no minimum elapsed time since the last submission, and no minimum number of votes that must have accumulated before a resubmission is permitted. The only guard is that the caller must be a current NNS subnet node operator: [3](#0-2) 

The `MAX_TIME_FOR_GOVERNANCE_UPGRADE_ROOT_PROPOSAL` (7 days) is enforced only during voting, not during submission: [4](#0-3) 

The canister endpoint is exposed as an `#[update]` method callable by any principal; the node-operator check is the sole access control: [5](#0-4) 

---

### Impact Explanation

A malicious NNS node operator can:

1. Submit a root proposal containing a malicious governance wasm.
2. Observe the accumulating "no" ballots from other node operators.
3. Resubmit (with any valid wasm targeting governance) immediately before `f+1` "no" votes are reached, atomically resetting the entire ballot set.
4. Repeat indefinitely.

The result is that the malicious proposal can never be formally rejected. It occupies the attacker's proposal slot permanently and forces all other node operators to re-cast their "no" votes after every reset. Because the `PROPOSALS` map grows by one entry per distinct node-operator proposer, this does not block other node operators from submitting and passing their own proposals — but it does mean the attacker's malicious proposal remains live and visible indefinitely, and any node operator who votes "yes" on it (e.g., through confusion or social engineering) has their vote immediately counted toward execution.

**Impact: Medium** — a single below-threshold node operator can permanently prevent rejection of their own proposal and force repeated re-voting by the rest of the committee.

---

### Likelihood Explanation

The NNS subnet has on the order of 13–40 node operators. Compromising or acting as a single malicious node operator is a realistic threat model (it is explicitly within the Byzantine fault model that the system is supposed to tolerate). The attack requires no special tooling beyond the ability to call `submit_root_proposal_to_upgrade_governance_canister` repeatedly, which is a public update endpoint on the root canister.

**Likelihood: Medium** — requires a single malicious NNS node operator; no majority, no admin key, no threshold corruption needed.

---

### Recommendation

Add a resubmission cooldown: once a proposal from a given principal has received any non-proposer ballot, disallow replacement until the proposal is either accepted, rejected, or expired. Alternatively, record the highest "no" vote count ever seen for a proposal and carry it forward on resubmission, so that resetting ballots does not erase the rejection signal. A simpler mitigation is to enforce a minimum interval (e.g., equal to `MAX_TIME_FOR_GOVERNANCE_UPGRADE_ROOT_PROPOSAL`) between successive submissions from the same principal.

---

### Proof of Concept

```
// Attacker is a registered NNS node operator (principal A).
// Other node operators begin voting "no" on A's proposal.

loop {
    // Watch vote tally via get_pending_root_proposals_to_upgrade_governance_canister()
    if no_votes_on_my_proposal() >= fault_threshold() {
        // Reset all ballots before rejection threshold is reached
        root_canister.submit_root_proposal_to_upgrade_governance_canister(
            current_governance_sha,
            malicious_change_canister_request,  // same or different wasm
        ).await;
        // All previously cast "no" votes are now gone; only A's auto-yes remains
    }
}
```

The loop is viable because each call to `submit_root_proposal_to_upgrade_governance_canister` is an ordinary ingress update message with no on-chain rate limit. The attacker can poll `get_pending_root_proposals_to_upgrade_governance_canister` (also an update, callable by anyone) to monitor the tally. [6](#0-5)

### Citations

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L27-27)
```rust
const MAX_TIME_FOR_GOVERNANCE_UPGRADE_ROOT_PROPOSAL: u64 = 60 * 60 * 24 * 7;
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L142-143)
```rust
thread_local! {
  static PROPOSALS: RefCell<BTreeMap<PrincipalId, GovernanceUpgradeRootProposal>> = const { RefCell::new(BTreeMap::new()) };
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L244-250)
```rust
    if voted_on == 0 {
        let message = format!(
            "{LOG_PREFIX}Invalid proposal. Caller: {caller} must be among the node operators of the nns subnet."
        );
        println!("{message}");
        return Err(message);
    }
```

**File:** rs/nns/handlers/root/impl/src/root_proposals.rs (L252-278)
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

**File:** rs/nns/handlers/root/impl/canister/canister.rs (L100-111)
```rust
#[update(hidden = true)]
async fn submit_root_proposal_to_upgrade_governance_canister(
    expected_governance_wasm_sha: serde_bytes::ByteBuf,
    proposal: ChangeCanisterRequest,
) -> Result<(), String> {
    ic_nns_handler_root::root_proposals::submit_root_proposal_to_upgrade_governance_canister(
        caller(),
        expected_governance_wasm_sha.to_vec(),
        proposal,
    )
    .await
}
```
