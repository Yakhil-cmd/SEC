Audit Report

## Title
Single NNS Node Operator Can Permanently Block Emergency Governance Upgrade via Proposal Vote Reset - (File: rs/nns/handlers/root/impl/src/root_proposals.rs)

## Summary

The `submit_root_proposal_to_upgrade_governance_canister` function unconditionally overwrites any existing pending proposal from the same caller, silently discarding all accumulated votes. A single NNS node operator — explicitly modeled as a potentially Byzantine peer — can exploit this to indefinitely reset the vote tally on any root proposal, permanently blocking the emergency governance upgrade path. This defeats the Byzantine fault tolerance the system was designed to provide, since the threshold requires N-f votes but a single proposer can reset progress at will.

## Finding Description

`PROPOSALS` is a `BTreeMap<PrincipalId, GovernanceUpgradeRootProposal>` keyed by proposer principal. When `submit_root_proposal_to_upgrade_governance_canister` is called by a principal that already has a pending proposal, lines 252–278 of `rs/nns/handlers/root/impl/src/root_proposals.rs` unconditionally overwrite the existing entry:

```rust
// Lines 252-278
PROPOSALS.with(|proposals| {
    if let Some(previous_proposal_from_the_same_principal) = proposals.borrow().get(&caller) {
        println!("{LOG_PREFIX}Current root proposal ... from {caller} is going to be overwritten.");
    }
    proposals.borrow_mut().insert(caller, GovernanceUpgradeRootProposal { ... });
});
```

There is no check for whether other node operators have already cast ballots on the existing proposal. The new proposal is initialized with only the proposer's own yes votes (line 236: `node_operator_ballots.push((node_operator_pid, RootProposalBallot::Yes))`), resetting all other ballots to `Undecided`.

The Byzantine majority threshold at lines 110–122 requires `votes_yes >= (num_nodes - max_faults)` where `max_faults = (num_nodes - 1) / 3`. For a 7-node NNS subnet this is 5 votes. The attacker can call `submit_root_proposal_to_upgrade_governance_canister` any time before the 5th yes vote is cast, resetting the tally to 1. There is no rate limiting, cooldown, or guard against this. The behavior is even documented at lines 166–169 as intentional, but without acknowledgment of the DoS implication.

The entry point is the public `#[update(hidden = true)]` method at `rs/nns/handlers/root/impl/canister/canister.rs` lines 100–111, callable by any NNS node operator with no additional access control beyond the node operator membership check.

## Impact Explanation

This maps to **High ($2,000–$10,000): Application/platform-level DoS or subnet availability impact not based on raw volumetric DDoS.** The root proposal mechanism is the sole emergency path to upgrade the NNS Governance canister when Governance itself cannot execute proposals. A single malicious node operator can permanently prevent any root proposal from reaching the execution threshold for as long as they continue resubmitting. This constitutes a denial of service against the IC's emergency governance upgrade mechanism — a high-stakes, irreplaceable path. No cryptographic primitive is broken; no majority is required; a single below-threshold Byzantine peer suffices.

## Likelihood Explanation

The attacker must be a legitimate NNS node operator, a restricted but non-trivial set. No special tooling is required beyond calling the public update method. The attack is only relevant during an emergency governance upgrade scenario (rare), but that is precisely when the mechanism is most critical. The attack is repeatable indefinitely at negligible cost. A single node operator is explicitly below the Byzantine fault threshold (f ≥ 2 for any NNS subnet of practical size), placing this squarely within the Byzantine peer model the system is designed to handle — but fails to handle here.

## Recommendation

1. **Reject resubmission if any other operator has already voted.** Before overwriting, check whether any ballot in `node_operator_ballots` is not `Undecided` for a principal other than the caller. If so, return an error.
2. **Alternatively, lock the proposal once the first external vote is cast.** Allow resubmission only when no external votes exist.
3. **Add a cooldown between successive submissions** from the same principal to limit the rate of resets even if resubmission is permitted.
4. **Emit a structured log or metric** when a proposal with accumulated votes is overwritten, so other node operators can detect the attack in progress.

## Proof of Concept

Using a local PocketIC or integration test environment with a simulated 7-node NNS subnet:

1. Node operator A calls `submit_root_proposal_to_upgrade_governance_canister` with `WASM_v1`. Proposal stored with 1 yes vote.
2. Node operators B, C, D each call `vote_on_root_proposal_to_upgrade_governance_canister` with yes. Tally: 4/7 (threshold: 5).
3. Before operator E votes, operator A calls `submit_root_proposal_to_upgrade_governance_canister` again (same or different WASM). The proposal is overwritten; tally resets to 1/7.
4. Operators B, C, D must vote again on the new proposal hash. Operator A repeats step 3 each time the tally approaches 5.
5. Assert via `get_pending_root_proposals_to_upgrade_governance_canister` that the proposal never reaches `is_byzantine_majority_yes()` returning true.
6. The governance upgrade is permanently blocked. The existing integration test suite at `rs/nns/integration_tests/src/root_proposals.rs` provides the scaffolding to implement this as a deterministic test.