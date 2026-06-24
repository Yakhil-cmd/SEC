Audit Report

## Title
Any Valid SNS Swap Canister Can Settle Another SNS's Neurons' Fund Participation with Attacker-Controlled `sns_governance_canister_id`, Redirecting Minted ICP - (File: rs/nns/governance/src/governance.rs)

## Summary
`settle_neurons_fund_participation` in NNS Governance authorizes the caller by verifying only that it is *any* valid SNS swap canister registered in SNS-W, not the specific swap canister associated with the proposal being settled. The `sns_governance_canister_id` field in a `Committed` request — which determines the ICP mint destination — is taken directly from caller-supplied input with no validation against the proposal's stored SNS deployment record. An attacker controlling a legitimate SNS swap canister can call this endpoint for a victim SNS's proposal, supply their own governance canister as the mint destination, steal the Neurons' Fund ICP, and permanently corrupt the victim SNS's settlement state.

## Finding Description
**Authorization checks class, not identity.**

At `rs/nns/governance/src/governance.rs` L7018–7038, the authorization logic calls `is_canister_id_valid_swap_canister_id`, which queries SNS-W's `list_deployed_snses` and checks whether the caller appears as *any* swap canister across all deployed SNS instances (L8215–8218). The code comment at L7018 explicitly acknowledges: *"Note that a Swap could settle each other's participation."* There is no assertion that `caller == swap_canister_id_for(request.nns_proposal_id)`.

**Caller-supplied `sns_governance_canister_id` used as mint destination without validation.**

At L7249–7273, when the result is `Committed`, the `sns_governance_canister_id` field is extracted directly from `request.swap_result` and passed to `mint_to_sns_governance`. Inside that function (L7495–7506), it is used as the ICP transfer destination:
```rust
let destination = AccountIdentifier::new(sns_governance_canister_id, /* subaccount = */ None);
let _ = self.ledger.transfer_funds(amount_icp_e8s, 0, None, destination, 0).await...;
```
The `sns_governance_canister_id` stored in the proposal's `CreateServiceNervousSystem` action or its deployed SNS record is never retrieved and compared against the caller-supplied value.

**Idempotency check permanently locks state.**

At L7166–7174, once `final_neurons_fund_participation` and `neurons_fund_refunds` are both set, the function returns the previously computed result immediately. An attacker who settles first permanently prevents the legitimate swap canister from re-settling.

**Exploit flow:**
1. Attacker creates SNS A through the NNS governance process, obtaining a valid `swap_a` canister ID registered in SNS-W.
2. Victim SNS B has an active swap with Neurons' Fund participation (`initial_neurons_fund_participation` set in its proposal data).
3. Attacker calls `settle_neurons_fund_participation` from `swap_a` with `nns_proposal_id = SNS_B_proposal`, `result = Committed { sns_governance_canister_id: gov_a, total_direct_participation_icp_e8s: <maximized> }`.
4. Authorization passes: `swap_a` is a valid swap canister in SNS-W.
5. Proposal check passes: SNS B's proposal is `CreateServiceNervousSystem`.
6. State machine check passes: first call, lifecycle not yet terminal.
7. NNS Governance computes Neurons' Fund participation using attacker-supplied `total_direct_participation_icp_e8s`.
8. NNS Governance mints ICP and sends it to `gov_a` (attacker-controlled).
9. SNS B's proposal lifecycle is set to `Committed` (terminal); idempotency check permanently blocks legitimate settlement.

## Impact Explanation
This is a **Critical** impact: theft, permanent loss, and illegal minting of ICP from the Neurons' Fund. The Neurons' Fund can hold and match substantial ICP (potentially millions of ICP depending on matched funding parameters). Affected NNS neurons lose their reserved maturity permanently with no SNS tokens issued in return. The victim SNS's settlement state is irreversibly corrupted without an NNS upgrade. This matches the allowed impact: *"Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles or in-scope chain-key/ledger assets, especially over $1M."*

## Likelihood Explanation
**Low-Medium.** The precondition is that the attacker must control a legitimate SNS swap canister, requiring a successful NNS governance vote to create an SNS. This is a meaningful but not insurmountable barrier for a sufficiently staked neuron holder. Once an SNS is created, the attack is a single canister call with no further preconditions. The attack window is the period between SNS B's swap committing and its legitimate `settle_neurons_fund_participation` call being processed — a race condition that an attacker can win by monitoring on-chain state and front-running.

## Recommendation
1. **Bind the caller to the proposal**: After fetching the proposal data for `nns_proposal_id`, retrieve the swap canister ID from the proposal's deployed SNS record and assert `caller == proposal_swap_canister_id`. This eliminates cross-swap settlement entirely.
2. **Validate `sns_governance_canister_id` against proposal data**: When processing a `Committed` result, retrieve the `sns_governance_canister_id` from the proposal's stored SNS deployment record and use that as the mint destination, ignoring the caller-supplied value entirely.

## Proof of Concept
```rust
// Attacker controls SNS A swap canister (canister ID: swap_a)
// Victim is SNS B with proposal_id = 42, governance = gov_b

// Call from swap_a to NNS Governance:
settle_neurons_fund_participation(SettleNeuronsFundParticipationRequest {
    nns_proposal_id: Some(42),  // SNS B's proposal
    result: Some(Result::Committed(Committed {
        sns_governance_canister_id: Some(gov_a),  // SNS A's governance (attacker-controlled)
        total_direct_participation_icp_e8s: Some(u64::MAX),  // maximize matched funding
        total_neurons_fund_participation_icp_e8s: Some(u64::MAX),
    })),
})
// Authorization: is swap_a in list_deployed_snses? YES → passes (L8215-8218)
// Proposal check: is proposal 42 a CreateServiceNervousSystem? YES → passes (L6997)
// State machine: first call, not terminal → proceeds (L7180-7185)
// mint_to_sns_governance called with sns_governance_canister_id = gov_a (L7266-7273)
// ICP transferred to gov_a (L7495-7506)
// Proposal 42 lifecycle set to Committed (terminal) → idempotency blocks SNS B (L7166-7174)
```
A deterministic integration test using PocketIC can reproduce this by deploying two SNS instances, having SNS A's swap canister call `settle_neurons_fund_participation` with SNS B's proposal ID and SNS A's governance canister as the destination, and asserting that ICP is minted to SNS A's governance canister and SNS B's subsequent legitimate settlement call is rejected.