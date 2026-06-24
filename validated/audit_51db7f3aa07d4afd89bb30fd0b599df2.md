Audit Report

## Title
Arbitrary SNS Swap Canister Can Redirect Neurons' Fund ICP Mint to Attacker-Controlled Destination - (File: rs/nns/governance/src/governance.rs)

## Summary

`settle_neurons_fund_participation` in NNS Governance authorizes any SNS Swap canister registered with SNS-W to settle any proposal's Neurons' Fund participation, without verifying that the caller is the specific Swap canister associated with the supplied `nns_proposal_id`. The `sns_governance_canister_id` used as the ICP mint destination is taken directly from the caller-supplied request payload rather than from stored `ProposalData`. A malicious SNS-B Swap canister can therefore settle SNS-A's Neurons' Fund participation, redirect the minted ICP to an attacker-controlled canister, and permanently lock SNS-A's proposal lifecycle in a terminal state.

## Finding Description

The public update method `settle_neurons_fund_participation` in `rs/nns/governance/canister/canister.rs` is callable by any canister. [1](#0-0) 

It delegates to `Governance::settle_neurons_fund_participation`. [2](#0-1) 

The authorization check at lines 7018–7038 calls `is_canister_id_valid_swap_canister_id`, which queries SNS-W's `list_deployed_snses` and checks only whether the caller's canister ID appears as *any* swap canister in the registry. The code comment at line 7018 explicitly acknowledges the gap: *"Note that a Swap could settle each other's participation."* [3](#0-2) 

The `is_canister_id_valid_swap_canister_id` implementation confirms this: it iterates all deployed SNS instances and returns `Ok` if the caller matches any `swap_canister_id`, with no binding to the specific proposal. [4](#0-3) 

After passing this check, the function reads `sns_governance_canister_id` directly from `request.swap_result` — a fully caller-controlled field — and passes it to `mint_to_sns_governance`. [5](#0-4) 

`mint_to_sns_governance` then mints ICP and transfers it to whatever `PrincipalId` was supplied. [6](#0-5) 

The `Committed` struct exposes `sns_governance_canister_id` as a fully caller-controlled field. [7](#0-6) 

The lifecycle lock set at line 7235 transitions the proposal to a terminal state (`Committed`) before the mint, permanently preventing any future legitimate settlement call from succeeding (the idempotency path at line 7166 returns the previously computed — attacker-influenced — result). [8](#0-7) 

There is no code path that reads `sns_governance_canister_id` from stored `ProposalData` and compares it against the caller-supplied value. The `execute_create_service_nervous_system_proposal` function does not persist the deployed swap canister ID into `ProposalData` after deployment, so no stored ground-truth exists to compare against. [9](#0-8) 

## Impact Explanation

This constitutes **theft of ICP** minted from the Neurons' Fund (maturity converted to ICP and sent to an attacker-controlled canister) and **permanent denial of service** for the legitimate SNS-A swap settlement (the proposal lifecycle is set to terminal, blocking all retries). The amount of ICP at risk equals the full Neurons' Fund matched participation for SNS-A's swap, which can be in the millions of ICP e8s for popular SNS launches. This matches the Critical impact class: *"Theft, permanent loss, illegal minting, or protocol insolvency involving exorbitant ICP/Cycles or in-scope chain-key/ledger assets."*

## Likelihood Explanation

The attacker must control a valid SNS Swap canister registered with SNS-W, which requires having successfully deployed an SNS through the NNS governance process. Given the number of active SNS deployments on mainnet, this precondition is realistic. The attack window exists between the moment SNS-A's swap reaches `Committed` lifecycle and the moment SNS-A's legitimate `finalize_swap` call completes `settle_neurons_fund_participation`. The attack is a single cross-canister call and is repeatable against any unsettled SNS with Neurons' Fund participation enabled.

## Recommendation

1. **Short term**: After the `is_canister_id_valid_swap_canister_id` check passes, use SNS-W's `get_deployed_sns_by_proposal_id` (which exists and maps proposal IDs to deployed SNS canister sets) to retrieve the canonical swap canister ID and governance canister ID for `request.nns_proposal_id`. Assert `caller == stored_swap_canister_id` and `request.sns_governance_canister_id == stored_sns_governance_canister_id` before proceeding to mint.

2. **Long term**: Persist the deployed SNS canister IDs (swap and governance) into `ProposalData` at execution time in `execute_create_service_nervous_system_proposal`, and derive all settlement parameters exclusively from that on-chain state. Treat the caller-provided `SettleNeuronsFundParticipationRequest` fields as advisory only (e.g., for the `total_direct_participation_icp_e8s` sanity check).

## Proof of Concept

1. Attacker deploys SNS-B through NNS governance, obtaining a valid SNS-B Swap canister ID registered with SNS-W.
2. SNS-A is a legitimate SNS with Neurons' Fund participation enabled, in `Open` or `Committed` lifecycle, not yet finalized (i.e., `final_neurons_fund_participation` is `None` in its `ProposalData`).
3. Attacker's SNS-B Swap canister calls NNS Governance `settle_neurons_fund_participation` with:
   ```
   nns_proposal_id = <SNS-A's CSS proposal ID>
   result = Committed {
       sns_governance_canister_id = <attacker-controlled canister principal>,
       total_direct_participation_icp_e8s = <any value>,
       total_neurons_fund_participation_icp_e8s = <any value>,
   }
   ```
4. NNS Governance passes the `is_canister_id_valid_swap_canister_id` check (SNS-B's swap is a valid swap canister).
5. The state machine check falls into "Ok case III" (`(Some(_), None, None)` with non-terminal lifecycle), proceeding to compute and mint.
6. NNS Governance sets SNS-A's proposal lifecycle to `Committed` (terminal), computes the Neurons' Fund ICP amount for SNS-A's proposal, and calls `mint_to_sns_governance` with the attacker-supplied `sns_governance_canister_id`.
7. ICP is minted to the attacker-controlled canister; SNS-A's proposal lifecycle is permanently terminal, blocking any future legitimate settlement.

A deterministic integration test using PocketIC can reproduce this by deploying two SNSes, having SNS-B's swap canister call `settle_neurons_fund_participation` with SNS-A's proposal ID and an attacker-controlled destination, and asserting that the ICP balance of the attacker canister increases and SNS-A's proposal lifecycle is `Committed`.

### Citations

**File:** rs/nns/governance/canister/canister.rs (L529-539)
```rust
#[update]
async fn settle_neurons_fund_participation(
    request: SettleNeuronsFundParticipationRequest,
) -> SettleNeuronsFundParticipationResponse {
    debug_log("settle_neurons_fund_participation");
    let response = governance_mut()
        .settle_neurons_fund_participation(caller(), request.into())
        .await;
    let intermediate = gov_pb::SettleNeuronsFundParticipationResponse::from(response);
    SettleNeuronsFundParticipationResponse::from(intermediate)
}
```

**File:** rs/nns/governance/src/governance.rs (L4569-4579)
```rust
        let proposal_data = self.mut_proposal_data_or_err(
            &proposal_id,
            "in execute_create_service_nervous_system_proposal",
        )?;
        Self::set_sns_token_swap_lifecycle_to_open(proposal_data);

        // subnet_id and canisters fields in deploy_new_sns_response are not
        // used. Would probably make sense to stick them on the
        // ProposalData.
        println!("deploy_new_sns succeeded: {:#?}", deploy_new_sns_response);
        Ok(())
```

**File:** rs/nns/governance/src/governance.rs (L6980-6985)
```rust
    pub async fn settle_neurons_fund_participation(
        &mut self,
        caller: PrincipalId,
        request: SettleNeuronsFundParticipationRequest,
    ) -> Result<NeuronsFundSnapshot, GovernanceError> {
        let request = ValidatedSettleNeuronsFundParticipationRequest::try_from(request)?;
```

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

**File:** rs/nns/governance/src/governance.rs (L7234-7237)
```rust
        // Set the lifecycle of the proposal to avoid interleaving callers.
        proposal_data.set_swap_lifecycle_by_settle_neurons_fund_participation_request_type(
            &request.swap_result,
        );
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

**File:** rs/nns/governance/src/governance.rs (L7495-7513)
```rust
        let destination =
            AccountIdentifier::new(sns_governance_canister_id, /* subaccount = */ None);

        let _ = self
            .ledger
            .transfer_funds(
                amount_icp_e8s,
                /* fee_e8s = */ 0, // Because there is no fee for minting.
                /* from_subaccount = */ None,
                destination,
                /* memo = */ 0,
            )
            .await
            .map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Minting ICP from the Neuron's Fund failed with error: {err:#?}"),
                )
            })?;
```

**File:** rs/nns/governance/src/governance.rs (L8191-8226)
```rust
async fn is_canister_id_valid_swap_canister_id(
    target_canister_id: CanisterId,
    env: &dyn Environment,
) -> Result<(), String> {
    let list_deployed_snses_response = env
        .call_canister_method(
            SNS_WASM_CANISTER_ID,
            "list_deployed_snses",
            Encode!(&ListDeployedSnsesRequest {}).expect(""),
        )
        .await
        .map_err(|err| {
            format!(
                "Failed to call the list_deployed_snses method on sns_wasm ({SNS_WASM_CANISTER_ID}): {err:?}",
            )
        })?;

    let list_deployed_snses_response =
        Decode!(&list_deployed_snses_response, ListDeployedSnsesResponse).map_err(|err| {
            format!(
                "Unable to decode response as ListDeployedSnsesResponse: {err}. reply_bytes = {list_deployed_snses_response:#?}",
            )
        })?;

    let is_swap = list_deployed_snses_response
        .instances
        .iter()
        .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
    if !is_swap {
        return Err(format!(
            "target_swap_canister_id is not the ID of any swap canister known to sns_wasm: {target_canister_id}"
        ));
    }

    Ok(())
}
```

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L3869-3880)
```rust
    pub struct Committed {
        /// This is where the minted ICP will be sent.
        #[prost(message, optional, tag = "1")]
        pub sns_governance_canister_id: ::core::option::Option<::ic_base_types::PrincipalId>,
        /// Total amount of participation from direct swap participants.
        #[prost(uint64, optional, tag = "2")]
        pub total_direct_participation_icp_e8s: ::core::option::Option<u64>,
        /// Total amount of participation from the Neurons' Fund.
        /// TODO\[NNS1-2570\]: Ensure this field is set.
        #[prost(uint64, optional, tag = "3")]
        pub total_neurons_fund_participation_icp_e8s: ::core::option::Option<u64>,
    }
```
