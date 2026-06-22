### Title
Any SNS Swap Canister Can Settle Neurons' Fund Participation on Behalf of Another SNS — (`rs/nns/governance/src/governance.rs`)

### Summary
The `settle_neurons_fund_participation` function in NNS Governance checks only that the caller is *any* valid SNS Swap canister registered in SNS-W, not that it is the *specific* Swap canister associated with the proposal being settled. This is the direct IC analog of the reported vulnerability: group-membership authorization instead of specific-identity authorization.

### Finding Description
In `rs/nns/governance/src/governance.rs`, the `settle_neurons_fund_participation` function performs the following authorization check: [1](#0-0) 

The function `is_canister_id_valid_swap_canister_id` queries SNS-W for all deployed SNS instances and checks whether the caller appears in that list: [2](#0-1) 

The check passes if the caller is **any** swap canister known to SNS-W. There is no check that the caller is the swap canister **associated with `request.nns_proposal_id`**. The code comment at line 7018 explicitly acknowledges this design choice: *"Note that a Swap could settle each other's participation."*

The `CreateServiceNervousSystem` proposal action does not embed the swap canister ID in the proposal data, so there is no stored binding to compare against: [3](#0-2) 

The public canister endpoint passes the raw caller directly into this logic without any additional guard: [4](#0-3) 

### Impact Explanation
A compromised or malicious SNS Swap canister (Swap A) can call NNS Governance's `settle_neurons_fund_participation` supplying a different SNS's `nns_proposal_id` (Swap B's proposal). Because the authorization check only verifies group membership, the call succeeds. Swap A can then:

1. **Report `Aborted` for a committed swap** — NNS Governance refunds all reserved Neurons' Fund maturity back to NNS neurons instead of minting ICP to SNS B's treasury. SNS B permanently loses its Neurons' Fund ICP allocation.
2. **Report `Committed` with a manipulated `sns_governance_canister_id`** — ICP is minted to Swap A's SNS governance canister instead of SNS B's, stealing the Neurons' Fund contribution.
3. **Report `Committed` with a manipulated `total_direct_participation_icp_e8s`** — The Matched Funding calculation for SNS B is corrupted, causing incorrect maturity burns and refunds across all Neurons' Fund neurons.

Once settled, the proposal's lifecycle is marked terminal and the settlement cannot be retried: [5](#0-4) 

### Likelihood Explanation
**Low.** SNS Swap canisters are deployed by SNS-W (an NNS-controlled canister) and are upgraded only via NNS governance proposals. For Swap A to make a cross-SNS settlement call, it must either (a) be upgraded with malicious code via a passed NNS proposal — requiring a governance majority — or (b) contain an exploitable bug in its own code that allows arbitrary inter-canister calls. Neither path is trivially reachable by an unprivileged actor. The design flaw is real and acknowledged in the source, but practical exploitation is constrained by the NNS governance trust model.

### Recommendation
Add a binding between the `nns_proposal_id` and the specific swap canister ID at proposal execution time (e.g., store the swap canister ID returned by `deploy_new_sns` in `ProposalData`). Then, in `settle_neurons_fund_participation`, verify that `caller == proposal_data.swap_canister_id` in addition to the existing SNS-W membership check. This mirrors the stricter check used by the now-obsolete `SettleCommunityFundParticipation` message, whose proto comment states: *"The caller's principal ID must match the value in the `target_swap_canister_id` field in the proposal."* [6](#0-5) 

### Proof of Concept
1. Two SNS instances exist: SNS-A (swap canister `swap_a`, proposal ID `prop_a`) and SNS-B (swap canister `swap_b`, proposal ID `prop_b`). Both are registered in SNS-W.
2. SNS-B's swap commits successfully. SNS-B's swap canister has not yet called `settle_neurons_fund_participation`.
3. SNS-A's swap canister (compromised or buggy) calls NNS Governance's `settle_neurons_fund_participation` with:
   - `nns_proposal_id = prop_b`
   - `result = Aborted`
4. NNS Governance checks: is `swap_a` a valid CanisterId? Yes. Is `swap_a` in SNS-W's list of swap canisters? Yes. Authorization passes.
5. NNS Governance refunds all Neurons' Fund maturity reserved for SNS-B and marks `prop_b`'s lifecycle as `Aborted`.
6. When SNS-B's legitimate swap canister later calls `settle_neurons_fund_participation` with `prop_b`, it hits the idempotency guard at line 7166 and returns the already-settled (Aborted) result — SNS-B's Neurons' Fund ICP is permanently lost. [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6991-7017)
```rust
        // Check that the action associated with this proposal is indeed CreateServiceNervousSystem.
        if let Some(action) = proposal_data
            .proposal
            .as_ref()
            .and_then(|p| p.action.as_ref())
        {
            if let Action::CreateServiceNervousSystem(_) = action {
                // All good.
            } else {
                return Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    format!(
                        "Proposal {:?} is not of type CreateServiceNervousSystem.",
                        proposal_data.id,
                    ),
                ));
            }
        } else {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Proposal {:?} is missing its action and cannot authorize {} to \
                    settle Neurons' Fund participation.",
                    proposal_data.id, caller
                ),
            ));
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

**File:** rs/nns/governance/src/governance.rs (L7166-7175)
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
            }
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

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L3714-3720)
```rust
pub struct SettleCommunityFundParticipation {
    /// The caller's principal ID must match the value in the
    /// target_swap_canister_id field in the proposal (more precisely, in the
    /// OpenSnsTokenSwap).
    #[prost(uint64, optional, tag = "1")]
    pub open_sns_token_swap_proposal_id: ::core::option::Option<u64>,
    /// Each of the possibilities here corresponds to one of two ways that a swap
```
