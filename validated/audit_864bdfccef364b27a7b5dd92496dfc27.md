Audit Report

## Title
Missing Proposal-Caller Binding in `settle_neurons_fund_participation` Allows Any SNS Swap to Settle Another SNS's Neurons' Fund Participation - (File: `rs/nns/governance/src/governance.rs`)

## Summary
`settle_neurons_fund_participation` verifies the caller is *a* valid SNS swap canister but never verifies it is the swap canister *associated with the supplied `nns_proposal_id`*. Any deployed SNS swap canister can call this function with an arbitrary victim proposal ID and attacker-controlled `sns_governance_canister_id`, causing NNS Governance to mint and transfer Neurons' Fund ICP to the attacker's governance canister and permanently locking the victim SNS's settlement state via the idempotency path.

## Finding Description

The authorization check at L7018–7038 converts the caller to a `CanisterId` and calls `is_canister_id_valid_swap_canister_id`, which queries SNS-W and returns `Ok(())` if the caller appears in *any* deployed SNS instance's `swap_canister_id` field. [1](#0-0) 

`is_canister_id_valid_swap_canister_id` performs only a set-membership check — it does not return which SNS instance the caller belongs to, and the caller never cross-checks that instance against `request.nns_proposal_id`. [2](#0-1) 

The developer comment at L7018 explicitly acknowledges the gap: *"Note that a Swap could settle each other's participation."* No subsequent binding check exists. [3](#0-2) 

After the authorization check, the function extracts `sns_governance_canister_id` directly from `request.swap_result` (fully attacker-controlled) and passes it to `mint_to_sns_governance` without any validation against the proposal's actual SNS governance canister. [4](#0-3) 

Once the mint succeeds, `final_neurons_fund_participation` is written to the proposal and the lifecycle is set to `Committed`. All subsequent calls hit the idempotency path and return the attacker-computed result, permanently preventing the legitimate swap canister from re-settling with correct parameters. [5](#0-4) 

## Impact Explanation

An attacker controlling any deployed SNS swap canister can:
1. Redirect the entire Neurons' Fund ICP reserve for a victim SNS swap to their own governance canister (illegal minting/theft of ICP).
2. Permanently corrupt the victim SNS's settlement state, causing its `finalize_swap` to proceed with wrong Neurons' Fund neuron data.

This matches the allowed Critical impact: *"Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles or in-scope chain-key/ledger assets."* The Neurons' Fund reserve for a single swap can be in the millions of ICP. The ICP is minted from Neurons' Fund maturity and sent to the wrong canister with no recovery path once the idempotency lock is set.

## Likelihood Explanation

The precondition is controlling a legitimately deployed SNS swap canister, which requires passing an NNS governance vote. This is a meaningful but not prohibitive barrier — any existing SNS project satisfies it. Once the attacker has a deployed SNS, the attack is a single inter-canister call with no additional setup. The attack window (between SNS B's swap opening and its `finalize_swap` call) can span days to weeks. The attack executes in one block and is not detectable before execution.

## Recommendation

After confirming the caller is a valid swap canister, additionally verify that the caller is the swap canister *associated with the supplied `nns_proposal_id`*. Concretely, extend `is_canister_id_valid_swap_canister_id` (or add a new helper) to return the matching `DeployedSns` instance, then assert that `instance.swap_canister_id == Some(caller.into())` for the instance whose `nns_proposal_id` matches `request.nns_proposal_id`. Additionally, validate that `request.sns_governance_canister_id` matches `instance.governance_canister_id` from SNS-W rather than accepting it as a free field from the caller.

## Proof of Concept

1. SNS B has an open swap with `nns_proposal_id = 42`; Neurons' Fund has reserved 100,000 ICP of maturity.
2. Attacker controls SNS A with swap canister `swap_a` and governance canister `gov_a` (legitimately deployed).
3. `swap_a` calls `settle_neurons_fund_participation` with:
   ```
   nns_proposal_id = 42
   result = Committed {
     sns_governance_canister_id = gov_a,
     total_direct_participation_icp_e8s = u64::MAX,
   }
   ```
4. `is_canister_id_valid_swap_canister_id` returns `Ok(())` — `swap_a` is a valid swap canister.
5. NNS Governance computes maximum Neurons' Fund participation, mints ICP, and sends it to `gov_a`.
6. Proposal 42's lifecycle is set to `Committed`; `final_neurons_fund_participation` is now `Some(attacker_result)`.
7. SNS B's legitimate swap canister later calls `settle_neurons_fund_participation` for proposal 42, hits the idempotency path at L7166–7174, and receives the attacker-computed result — SNS B's Neurons' Fund neurons are created with wrong amounts and the stolen ICP is unrecoverable.

A deterministic integration test can reproduce this using PocketIC: deploy two SNS instances, open a swap for SNS B, call `settle_neurons_fund_participation` from SNS A's swap canister with SNS B's proposal ID and SNS A's governance canister as destination, then assert that SNS A's governance canister received ICP and that SNS B's subsequent settlement call returns the attacker-controlled snapshot.

### Citations

**File:** rs/nns/governance/src/governance.rs (L7018-7038)
```rust
        // Check authorization. Note that a Swap could settle each other's participation.
        let target_canister_id: CanisterId = caller.try_into().map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "Caller {caller} is not a valid CanisterId and is not authorized to \
                        settle Neuron's Fund participation in a decentralization swap. Err: {err:?}",
                ),
            )
        })?;
        if let Err(err_msg) =
            is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::NotAuthorized,
                format!(
                    "Caller {caller} is not authorized to settle Neurons' Fund \
                    participation in a decentralization swap. Err: {err_msg:?}",
                ),
            ));
        }
```

**File:** rs/nns/governance/src/governance.rs (L7166-7174)
```rust
            (Some(_), Some(_), Some(previously_computed_final_neurons_fund_participation)) => {
                // Ok case I: Return the priorly computed results (this is an idempotent function).
                println!(
                    "{}INFO: settle_neurons_fund_participation was called for a swap \
                        that has already been settled with ProposalId {:?}. Returning without \
                        doing additional work.",
                    LOG_PREFIX, proposal_data.id
                );
                return Ok(previously_computed_final_neurons_fund_participation.into_snapshot());
```

**File:** rs/nns/governance/src/governance.rs (L7249-7273)
```rust
        } else if let SwapResult::Committed {
            sns_governance_canister_id,
            total_neurons_fund_participation_icp_e8s:
                swap_estimated_total_neurons_fund_participation_icp_e8s,
            ..
        } = request.swap_result
        {
            println!(
                "{}INFO: The Neurons' Fund has decided to provide Matched Funding to the \
                SNS created via proposal {:?}, in the amount of {} ICP e8s taken from {} \
                of its neurons. Congratulations!",
                LOG_PREFIX,
                request.nns_proposal_id,
                amount_icp_e8s,
                final_neurons_fund_participation.num_neurons(),
            );

            let mint_icp_result = self
                .mint_to_sns_governance(
                    &request.nns_proposal_id,
                    sns_governance_canister_id,
                    swap_estimated_total_neurons_fund_participation_icp_e8s,
                    amount_icp_e8s,
                )
                .await;
```

**File:** rs/nns/governance/src/governance.rs (L8215-8223)
```rust
    let is_swap = list_deployed_snses_response
        .instances
        .iter()
        .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
    if !is_swap {
        return Err(format!(
            "target_swap_canister_id is not the ID of any swap canister known to sns_wasm: {target_canister_id}"
        ));
    }
```
