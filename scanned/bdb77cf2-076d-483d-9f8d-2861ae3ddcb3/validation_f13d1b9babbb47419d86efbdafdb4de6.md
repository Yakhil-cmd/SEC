### Title
Any Deployed SNS Swap Canister Can Settle Another SNS's Neurons' Fund Participation - (File: rs/nns/governance/src/governance.rs)

### Summary

`settle_neurons_fund_participation` in NNS Governance authorizes the caller only by checking that it is *any* valid SNS swap canister, without verifying that the caller is the *specific* swap canister associated with the `nns_proposal_id` in the request. This is the direct IC analog of the zNS `approveDomainBid` bug: any authorized member of a class (any swap canister / any parent domain owner) can act on resources belonging to a different member of that class.

### Finding Description

The public update endpoint `settle_neurons_fund_participation` in `rs/nns/governance/canister/canister.rs` passes `caller()` and the raw request to `Governance::settle_neurons_fund_participation`. [1](#0-0) 

Inside that function, the authorization check calls `is_canister_id_valid_swap_canister_id`, which queries SNS-W's `list_deployed_snses` and returns `Ok` if the caller appears as *any* swap canister in the global list — regardless of which proposal it was created by. [2](#0-1) 

The code itself acknowledges the gap with the comment: `// Check authorization. Note that a Swap could settle each other's participation.` [3](#0-2) 

`is_canister_id_valid_swap_canister_id` only checks membership in the global swap set, never binding the caller to the specific proposal: [4](#0-3) 

After passing this check, the function uses the caller-supplied `sns_governance_canister_id` field from the `Committed` variant as the ICP mint destination: [5](#0-4) 

The `Committed` struct's `sns_governance_canister_id` is fully attacker-controlled — it is a free field in the request, not derived from on-chain proposal state: [6](#0-5) 

### Impact Explanation

A malicious SNS swap canister (Swap A, legitimately deployed) can call `settle_neurons_fund_participation` with:
- `nns_proposal_id` = the proposal ID of a *victim* SNS B that has Neurons' Fund participation reserved but not yet settled
- `result = Committed { sns_governance_canister_id: <attacker-controlled address>, total_direct_participation_icp_e8s: <maximized value> }`

NNS Governance will then:
1. **Mint ICP** (from burned NNS neuron maturity) to the attacker-supplied `sns_governance_canister_id`, stealing the Neurons' Fund contribution intended for SNS B.
2. **Permanently mark SNS B's proposal lifecycle as `Committed`** via `set_swap_lifecycle_by_settle_neurons_fund_participation_request_type`. The idempotency guard at line 7166 will then return the attacker's forged result on all subsequent calls, permanently blocking SNS B's legitimate swap canister from ever settling.
3. **Burn NNS neuron maturity** of Neurons' Fund participants without creating the corresponding SNS neurons for them in SNS B.

This is a direct theft of ICP minted from NNS neuron maturity and a permanent freeze of the victim SNS's Neurons' Fund settlement. [7](#0-6) 

### Likelihood Explanation

**Preconditions:**
1. The attacker must control a valid SNS swap canister — achievable by submitting a legitimate `CreateServiceNervousSystem` NNS proposal (open to any NNS neuron holder with sufficient stake).
2. A victim SNS with Neurons' Fund participation must exist with an unsettled proposal.

Both conditions are routinely met on mainnet whenever multiple SNS launches are in progress. The attacker's SNS proposal does not need to succeed or commit; the swap canister is registered in SNS-W as soon as it is deployed, which is sufficient to pass the authorization check.

### Recommendation

Bind the authorization check to the specific proposal: store the swap canister ID on-chain in the `ProposalData` when the `CreateServiceNervousSystem` proposal executes, and in `settle_neurons_fund_participation` verify that `caller == proposal_data.swap_canister_id` rather than checking global SNS-W membership.

Additionally, derive `sns_governance_canister_id` from the on-chain `ProposalData` (where it is recorded at SNS deployment time) instead of trusting the caller-supplied field in the `Committed` request.

### Proof of Concept

1. Attacker submits a `CreateServiceNervousSystem` NNS proposal and gets a valid SNS swap canister `swap_A` deployed by SNS-W.
2. Victim SNS B has proposal `P_B` with Neurons' Fund participation reserved (lifecycle = `Open`).
3. `swap_A` calls NNS Governance's `settle_neurons_fund_participation` with:
   ```
   SettleNeuronsFundParticipationRequest {
       nns_proposal_id: Some(P_B),
       result: Some(Committed {
           sns_governance_canister_id: Some(<attacker_wallet>),
           total_direct_participation_icp_e8s: Some(u64::MAX),  // maximize NF contribution
           total_neurons_fund_participation_icp_e8s: Some(0),
       }),
   }
   ```
4. `is_canister_id_valid_swap_canister_id(swap_A, ...)` returns `Ok` because `swap_A` is in SNS-W's list. [8](#0-7) 
5. NNS Governance mints ICP to `<attacker_wallet>` and marks `P_B` as `Committed`. [9](#0-8) 
6. SNS B's legitimate `swap_B` later calls `settle_neurons_fund_participation(P_B, Committed {...})` and hits the idempotency guard, receiving the attacker's forged snapshot — NF maturity is permanently burned, ICP is in the attacker's wallet, and SNS B's NF participants receive no SNS neurons. [7](#0-6)

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

**File:** rs/nns/governance/src/governance.rs (L8191-8225)
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
```

**File:** rs/nns/governance/api/src/types.rs (L3547-3555)
```rust
    pub struct Committed {
        /// This is where the minted ICP will be sent.
        pub sns_governance_canister_id: Option<PrincipalId>,
        /// Total amount of participation from direct swap participants.
        pub total_direct_participation_icp_e8s: Option<u64>,
        /// Total amount of participation from the Neurons' Fund.
        /// TODO\[NNS1-2570\]: Ensure this field is set.
        pub total_neurons_fund_participation_icp_e8s: Option<u64>,
    }
```
