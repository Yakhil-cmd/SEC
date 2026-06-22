### Title
Premature Proposal Deletion Before Wasm Validation Permanently Destroys a Majority-Approved Root Governance Upgrade - (File: `rs/nns/handlers/root/impl/src/root_proposals.rs`)

### Summary

In `vote_on_root_proposal_to_upgrade_governance_canister`, the pending `GovernanceUpgradeRootProposal` is irrevocably removed from the `PROPOSALS` map **before** the final async wasm-SHA safety check completes. If that check fails — which happens naturally when two concurrent root proposals both reach Byzantine majority and the first one executes first — the second proposal is permanently destroyed with no upgrade performed and no way to retry without re-collecting all votes.

### Finding Description

`vote_on_root_proposal_to_upgrade_governance_canister` in `rs/nns/handlers/root/impl/src/root_proposals.rs` follows this sequence when a Byzantine majority YES is detected:

1. **Line 408** — the proposal is unconditionally removed from canister state:
   ```rust
   PROPOSALS.with(|proposals| proposals.borrow_mut().remove(&proposer));
   ```
2. **Line 411** — an inter-canister `await` is issued to fetch the current governance wasm hash:
   ```rust
   let current_governance_wasm_sha = get_current_governance_canister_wasm().await;
   ```
3. **Lines 412–419** — if the fetched hash does not match `proposal.current_wasm_sha`, the function returns `Err(...)` — but the proposal has already been permanently deleted from state.

The comment on line 382–383 ("Once the proposal is accepted or rejected it's state is final, so it's ok to clone and execute from the clone") was written assuming the wasm check would always pass, but the check can legitimately fail when a concurrent proposal has already upgraded the governance canister between the `remove` and the `await`.

This is the exact "delete before all checks are done" pattern described in the reference report: a one-time-use state entry (`PROPOSALS[proposer]`) is consumed on the first check, so a subsequent check that should gate the action instead finds nothing to act on.

### Impact Explanation

A fully-voted, Byzantine-majority-approved root governance upgrade proposal is permanently destroyed without executing the upgrade. The `PROPOSALS` map is stored only in heap memory (not stable storage), so there is no recovery path. All participating node operators must re-submit and re-vote on a new proposal. Because the root canister is the only path to upgrade the NNS governance canister outside of normal NNS voting, this constitutes a denial-of-service against the emergency governance upgrade mechanism. In a scenario where the governance canister is broken and the root-proposal path is the only recovery route, this bug can permanently block recovery.

### Likelihood Explanation

The trigger condition is two concurrent root proposals both reaching Byzantine majority. This is a realistic operational scenario: the NNS subnet has ~40 nodes; node operators are independent parties who may independently decide to submit upgrade proposals for the same governance wasm. The async gap between line 408 (`remove`) and line 411 (`await`) is a real inter-canister call window during which the IC message scheduler can deliver other messages, including the execution of the first proposal. No malicious actor is required — two honest node operators acting independently are sufficient.

### Recommendation

Move the `PROPOSALS.with(|proposals| proposals.borrow_mut().remove(&proposer))` call to **after** the wasm check passes, so the proposal is only deleted when the upgrade is actually going to proceed:

```rust
if proposal.is_byzantine_majority_yes() {
    let payload = proposal.payload.clone();
    // Do NOT remove here — check first
    let current_governance_wasm_sha = get_current_governance_canister_wasm().await;
    if current_governance_wasm_sha != proposal.current_wasm_sha {
        // Proposal is stale; now it is safe to delete it
        PROPOSALS.with(|proposals| proposals.borrow_mut().remove(&proposer));
        return Err(message);
    }
    // All checks passed — now remove and execute
    PROPOSALS.with(|proposals| proposals.borrow_mut().remove(&proposer));
    let _ = change_canister(payload).await;
    Ok(())
}
```

### Proof of Concept

1. Node operator **A** submits a root proposal targeting governance wasm `sha_v1` → stored as `PROPOSALS[A]`.
2. Node operator **B** submits a root proposal also targeting `sha_v1` → stored as `PROPOSALS[B]`.
3. Enough voters cast YES on proposal A; the final voter's call enters `vote_on_root_proposal_to_upgrade_governance_canister`:
   - **Line 408**: `PROPOSALS[A]` is removed.
   - **Line 411**: async call returns `sha_v1`.
   - **Line 412**: `sha_v1 == sha_v1` → passes.
   - **Line 421**: governance canister is upgraded; wasm is now `sha_v2`.
4. Enough voters cast YES on proposal B; the final voter's call enters the same function:
   - **Line 408**: `PROPOSALS[B]` is **removed** — permanently gone.
   - **Line 411**: async call returns `sha_v2` (governance was already upgraded).
   - **Line 412**: `sha_v2 != sha_v1` → **fails**.
   - Function returns `Err(...)`. Proposal B is permanently destroyed; the intended upgrade to `sha_v2`-target wasm never executes.

The root cause is at: [1](#0-0) 

The proposal is removed at line 408 before the async wasm check at line 411, mirroring the original report's pattern of consuming a one-time-use approval before all validation steps are complete. [2](#0-1)

### Citations

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
