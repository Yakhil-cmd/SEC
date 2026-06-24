Audit Report

## Title
Cross-SNS Neurons' Fund Settlement Allows Arbitrary ICP Minting to Attacker-Controlled Address — (`rs/nns/governance/src/governance.rs`)

## Summary

`settle_neurons_fund_participation` in NNS Governance authorizes any registered SNS swap canister to settle *any* CSNS proposal's Neurons' Fund participation. The caller-supplied `sns_governance_canister_id` in the `Committed` result is used directly as the ICP mint destination without being validated against the SNS governance canister actually associated with the target proposal. An attacker controlling a legitimate SNS swap canister can supply a victim proposal's `nns_proposal_id` alongside an attacker-controlled `sns_governance_canister_id`, causing NNS Governance to mint the full Neurons' Fund participation amount to an arbitrary address and permanently blocking the legitimate SNS from ever settling its own participation.

## Finding Description

**Root cause — two independent checks that are never cross-correlated:**

**1. Caller authorization** (`governance.rs`, lines 7018–7038):

```rust
// Check authorization. Note that a Swap could settle each other's participation.
if let Err(err_msg) =
    is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
```

`is_canister_id_valid_swap_canister_id` (lines 8191–8226) calls `SNS_WASM.list_deployed_snses()` and checks only that the caller appears as *any* swap canister in the global registry:

```rust
let is_swap = list_deployed_snses_response
    .instances
    .iter()
    .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
```

There is no check that the caller is the swap canister associated with the specific `nns_proposal_id` in the request. The comment at line 7018 explicitly acknowledges this: *"Note that a Swap could settle each other's participation."*

**2. Mint destination** (`governance.rs`, lines 7249–7273):

```rust
} else if let SwapResult::Committed {
    sns_governance_canister_id,
    ...
} = request.swap_result
{
    let mint_icp_result = self
        .mint_to_sns_governance(
            &request.nns_proposal_id,
            sns_governance_canister_id,   // ← taken directly from attacker-controlled request
            ...
        )
        .await;
```

`mint_to_sns_governance` (lines 7495–7496) constructs the destination as `AccountIdentifier::new(sns_governance_canister_id, None)` and mints directly to it. The `sns_governance_canister_id` is never compared against the governance canister ID stored in SNS-W for the proposal being settled.

**State machine does not prevent the attack:**

The idempotency/lifecycle state machine (lines 7112–7186) only prevents *re-settlement* of an already-settled proposal (Ok case I, line 7166). For a victim proposal in its first-call state — `initial_neurons_fund_participation` set, `final_neurons_fund_participation` None, lifecycle non-terminal — the code falls through to "Ok case III" (line 7180) and proceeds to mint without any caller-to-proposal binding check.

**Exploit flow:**

1. Attacker controls SNS-1's swap canister (legitimately registered in SNS-W).
2. Victim SNS-2 has an active CSNS proposal with `initial_neurons_fund_participation` set and lifecycle `Open`.
3. Attacker calls `settle_neurons_fund_participation` with:
   - `nns_proposal_id` = SNS-2's proposal ID
   - `swap_result = Committed { sns_governance_canister_id = attacker_wallet, total_direct_participation_icp_e8s = max_direct }`
4. `is_canister_id_valid_swap_canister_id` passes — SNS-1's swap IS in `list_deployed_snses`.
5. State machine check passes — Ok case III (first call, non-terminal lifecycle).
6. `mint_to_sns_governance` mints the full computed Neurons' Fund participation to `attacker_wallet`.
7. SNS-2's proposal lifecycle is set to `Committed`, permanently blocking SNS-2's legitimate swap from ever settling.

## Impact Explanation

This constitutes **illegal ICP minting** to an attacker-controlled address. The Neurons' Fund participation for a single SNS swap can be substantial (potentially millions of ICP, bounded by the Neurons' Fund's total maturity and the attacker-supplied `total_direct_participation_icp_e8s`). This matches the Critical impact class: *"Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles or in-scope chain-key/ledger assets, especially over $1M."* The secondary effect — permanently blocking the victim SNS's legitimate settlement — constitutes additional protocol-level harm to NNS governance and the SNS framework.

## Likelihood Explanation

**Required preconditions:**

- The attacker must control a registered SNS swap canister. This requires either (a) successfully deploying an SNS via a CSNS proposal that passes NNS governance vote, or (b) compromising an existing SNS's swap canister. Path (a) is a meaningful but not insurmountable barrier — legitimate SNSes are deployed regularly on mainnet, and the NNS governance process is open.
- A victim CSNS proposal must be in `Open` lifecycle with Neurons' Fund participation enabled — this is the normal state for any active SNS swap.

Once preconditions are met, the attack is a single inter-canister call requiring no privileged NNS access, no private keys, and no majority corruption. It is repeatable against any active CSNS proposal. The severity is Critical; the likelihood is constrained by the SNS deployment precondition, placing this at the upper end of High to Critical.

## Recommendation

In `settle_neurons_fund_participation`, after the caller is verified as a valid swap canister, additionally verify that the caller's canister ID matches the swap canister ID associated with the specific `nns_proposal_id` being settled. This can be done by calling `SNS_WASM.get_deployed_sns_by_proposal_id(nns_proposal_id)` and comparing the returned `swap_canister_id` against `caller`. Additionally, the `sns_governance_canister_id` in the `Committed` message must be validated against the `governance_canister_id` returned by the same SNS-W lookup, rather than being accepted as caller-supplied input. This eliminates both the cross-SNS authorization gap and the arbitrary mint-destination vector in a single fix.

## Proof of Concept

State-machine integration test (PocketIC or replica):

1. Deploy two SNSes (SNS-1 and SNS-2) via two CSNS proposals, both with Neurons' Fund participation enabled. Both swaps are in `Open` lifecycle. Both proposals have `initial_neurons_fund_participation` set.
2. From SNS-1's swap canister principal, call NNS Governance's `settle_neurons_fund_participation` with:
   - `nns_proposal_id` = SNS-2's CSNS proposal ID
   - `swap_result = Committed { sns_governance_canister_id = attacker_principal, total_direct_participation_icp_e8s = max_direct_participation_icp_e8s }`
3. Assert: the call returns `Ok(...)` (not an authorization error).
4. Assert: ICP balance of `attacker_principal` increases by the Neurons' Fund participation amount computed from SNS-2's `initial_neurons_fund_participation`.
5. Assert: SNS-2's CSNS proposal `sns_token_swap_lifecycle` is now `Committed`.
6. Assert: SNS-2's legitimate swap canister calling `settle_neurons_fund_participation` now returns the idempotent cached result (Ok case I), having never minted to SNS-2's actual governance canister.