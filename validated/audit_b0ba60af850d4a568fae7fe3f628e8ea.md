Audit Report

## Title
Authorization Bypass in `settle_neurons_fund_participation` Allows Any SNS Swap to Redirect Neurons' Fund ICP to Arbitrary Governance Canister - (`rs/nns/governance/src/governance.rs`)

## Summary

`settle_neurons_fund_participation` in NNS Governance authorizes any registered SNS Swap canister to settle the Neurons' Fund participation for any NNS proposal, not just the proposal associated with the calling swap canister. The `sns_governance_canister_id` ICP mint destination is taken verbatim from the caller-supplied request with no cross-validation against the proposal's stored SNS governance canister. A malicious SNS Swap operator can race the victim SNS's `finalize_swap` call, redirect Neurons' Fund ICP to their own governance treasury, and leave the victim SNS with a cached success response but no ICP.

## Finding Description

**Authorization check (L7018–7038):** After verifying the proposal action is `CreateServiceNervousSystem`, the function calls `is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env)`. This helper queries SNS-W's `list_deployed_snses` and checks only that the caller appears as *any* swap canister in the global registry. It does not verify that the caller is the swap canister associated with the specific `nns_proposal_id` in the request. The code itself acknowledges this with the comment: `// Note that a Swap could settle each other's participation.`

**No per-proposal binding:** `ProposalData` does not store the swap canister ID or SNS governance canister ID assigned at deployment time. The `CreateServiceNervousSystem` action contains only pre-deployment parameters (name, token distribution, swap parameters, etc.) — the actual canister IDs are assigned by SNS-W at execution time and are not written back into `ProposalData`. There is therefore no stored value to cross-check the caller against.

**Caller-supplied mint destination (L7249–7277):** In the `Committed` branch, `sns_governance_canister_id` is destructured directly from `request.swap_result` and passed to `mint_to_sns_governance`. No comparison is made against any proposal-stored value.

**Idempotency lock (L7166–7174, L7234–7237):** Once settlement completes, `sns_token_swap_lifecycle` is set to a terminal value and `final_neurons_fund_participation` is stored. Any subsequent call for the same proposal hits "Ok case I" and returns the cached snapshot immediately. The victim SNS's legitimate `finalize_swap` → `settle_neurons_fund_participation` call therefore receives a success response even though ICP was sent to the attacker's canister.

**Exploit flow:**
1. Attacker controls SNS-A (legitimately deployed; swap = `A_swap`, governance = `A_gov`).
2. Victim SNS-B has proposal `P_B` in committed state, Neurons' Fund participation not yet settled.
3. `A_swap` calls `nns_governance.settle_neurons_fund_participation({ nns_proposal_id: P_B, result: Committed { sns_governance_canister_id: A_gov, ... } })`.
4. Governance verifies `A_swap` is in SNS-W registry → passes. Looks up `P_B`, finds `CreateServiceNervousSystem` → passes. Computes Neurons' Fund participation for `P_B`. Sets `P_B` lifecycle to Committed. Mints ICP to `A_gov`.
5. `B_swap` later calls `settle_neurons_fund_participation` for `P_B` → hits idempotency path, returns cached success. ICP was already sent to `A_gov`.

## Impact Explanation

This constitutes unauthorized theft and permanent loss of ICP from the Neurons' Fund. NNS neurons' maturity is permanently consumed for the wrong SNS, and the minted ICP is delivered to the attacker's governance treasury rather than the victim's. The victim SNS receives a success response but no ICP, constituting both a ledger conservation violation and a governance authorization bypass. This matches the **High** impact category: "Unauthorized access to neurons, governance assets, wallets, identities, ledgers, or canister-controlled funds" and "Significant NNS, SNS, or infrastructure security impact with concrete user or protocol harm." The amount stolen is bounded by the Neurons' Fund participation in the specific swap, which for large swaps can be substantial (tens of thousands of ICP).

## Likelihood Explanation

The attacker must control a legitimately deployed SNS Swap canister, which requires an NNS governance proposal to pass. This is a meaningful but not prohibitive constraint — many SNSes are already deployed on mainnet, and any of their swap canister operators satisfy this precondition today. The attack requires a single inter-canister call timed before the victim SNS's `finalize_swap` executes `settle_neurons_fund_participation`. The proposal ID of any SNS swap is public on-chain. No key compromise, threshold attack, or social engineering is required beyond operating a deployed SNS.

## Recommendation

1. **Bind settlement to the specific proposal's swap canister:** After the SNS is deployed, store the assigned swap canister ID in `ProposalData` (e.g., in a new field or within `neurons_fund_data`). In `settle_neurons_fund_participation`, after looking up `proposal_data`, verify that `caller` equals the stored swap canister ID for that proposal.

2. **Validate `sns_governance_canister_id` against proposal data:** Similarly store the assigned SNS governance canister ID in `ProposalData` at deployment time and reject any `Committed` request whose `sns_governance_canister_id` does not match.

3. **Alternatively, derive the governance canister ID from SNS-W:** Instead of accepting `sns_governance_canister_id` from the caller, look up the SNS instance in `list_deployed_snses` by matching the caller's swap canister ID, and use the `governance_canister_id` from that registry entry as the mint destination.

## Proof of Concept

A deterministic PocketIC or state-machine integration test can prove this:

1. Deploy NNS with Neurons' Fund neurons having maturity.
2. Deploy SNS-A (attacker) and SNS-B (victim) via `CreateServiceNervousSystem` proposals. Record `P_B`, `A_swap`, `A_gov`, `B_gov`.
3. Advance SNS-B's swap to `Committed` state with sufficient direct participation.
4. Before calling `B_swap.finalize_swap()`, call `nns_governance.settle_neurons_fund_participation` as `A_swap` with `nns_proposal_id = P_B` and `sns_governance_canister_id = A_gov`.
5. Assert: ICP ledger balance of `A_gov` increased by the Neurons' Fund participation amount; `B_gov` balance unchanged; `P_B` lifecycle is terminal.
6. Now call `B_swap.finalize_swap()` → `settle_neurons_fund_participation` for `P_B` returns cached success.
7. Assert: `B_gov` ICP balance is still zero (ICP was stolen by `A_gov`).