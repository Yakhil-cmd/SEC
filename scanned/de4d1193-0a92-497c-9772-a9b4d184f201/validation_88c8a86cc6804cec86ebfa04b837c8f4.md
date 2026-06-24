### Title
Any Deployed SNS Swap Canister Can Settle Another SNS's Neurons' Fund Participation — (`rs/nns/governance/src/governance.rs`)

### Summary

The `settle_neurons_fund_participation` function in NNS Governance checks only that the caller is *any* valid SNS Swap canister registered with SNS-W, but does not verify that the caller is the *specific* Swap canister associated with the `nns_proposal_id` supplied in the request. This is the direct IC analog of the "routers can access components from other routers" isolation failure: one SNS Swap canister can trigger settlement — including ICP minting and Neurons' Fund maturity refunds — for a *different* SNS's proposal.

### Finding Description

In `settle_neurons_fund_participation`, the authorization check is performed in two independent steps:

1. The `nns_proposal_id` in the request is used to look up the proposal and verify it is a `CreateServiceNervousSystem` action.
2. The caller is verified to be *any* swap canister known to SNS-W via `is_canister_id_valid_swap_canister_id`. [1](#0-0) 

The code comment at line 7018 explicitly acknowledges this: `// Check authorization. Note that a Swap could settle each other's participation.` [2](#0-1) 

The `is_canister_id_valid_swap_canister_id` helper only checks membership in the global list of all deployed SNS swap canisters — it does not bind the caller to the specific proposal: [3](#0-2) 

There is no check that the caller's canister ID matches the swap canister recorded in the proposal data for `nns_proposal_id`. The proposal data does not appear to store the swap canister ID in a way that is cross-checked here.

### Impact Explanation

A malicious or buggy SNS Swap canister (SNS-B) can call `settle_neurons_fund_participation` on NNS Governance with the `nns_proposal_id` of a *different* SNS (SNS-A) that has Neurons' Fund participation. If SNS-B sends `Committed` with a fabricated `sns_governance_canister_id` pointing to SNS-B's own governance, NNS Governance will:

- Mint ICP from the Neurons' Fund and transfer it to SNS-B's governance account (not SNS-A's).
- Consume the maturity reserved for SNS-A's Neurons' Fund neurons.
- Mark SNS-A's proposal lifecycle as `Committed`, preventing legitimate settlement.

This is a **governance authorization bug** and **ledger conservation bug**: Neurons' Fund ICP (backed by real NNS neuron maturity) is minted and redirected to the wrong SNS treasury. The Neurons' Fund neurons lose maturity without receiving the correct SNS tokens in return.

### Likelihood Explanation

Every deployed SNS Swap canister is a valid caller. SNS canisters are deployed permissionlessly via NNS proposals and their Wasm can be upgraded via NNS proposals, but the SNS Swap canister code itself runs on-chain and is not directly controlled by the SNS's own governance after deployment. A buggy SNS Swap (e.g., one with a reentrancy or logic bug in `finalize_swap`) could trigger this cross-SNS settlement accidentally. A deliberately malicious SNS Swap (deployed via a governance proposal that passes) could exploit it intentionally. The attack path is a direct canister-to-canister call from any registered SNS Swap to NNS Governance — no privileged key or threshold corruption is required.

### Recommendation

After the `is_canister_id_valid_swap_canister_id` check passes, add a binding check: verify that the caller's canister ID matches the swap canister ID stored in the proposal data for `nns_proposal_id`. The proposal data for a `CreateServiceNervousSystem` proposal should record the deployed swap canister ID (available from SNS-W's `list_deployed_snses` response), and `settle_neurons_fund_participation` should assert:

```rust
// After is_canister_id_valid_swap_canister_id passes:
let expected_swap_id = proposal_data.swap_canister_id_for_this_proposal()?;
if target_canister_id != expected_swap_id {
    return Err(GovernanceError::new_with_message(
        ErrorType::NotAuthorized,
        format!("Caller {caller} is the swap canister for a different SNS, not for proposal {:?}.", request.nns_proposal_id),
    ));
}
```

Alternatively, look up the SNS instance associated with `nns_proposal_id` from SNS-W and compare its `swap_canister_id` to the caller.

### Proof of Concept

1. SNS-A is deployed via proposal `P_A` with Neurons' Fund participation. Its swap canister is `swap_A`.
2. SNS-B is deployed via proposal `P_B`. Its swap canister is `swap_B`.
3. `swap_B` calls NNS Governance's `settle_neurons_fund_participation` with:
   - `nns_proposal_id = P_A` (SNS-A's proposal)
   - `result = Committed { sns_governance_canister_id = gov_B, ... }`
4. NNS Governance checks: is `P_A` a `CreateServiceNervousSystem` proposal? Yes. Is `swap_B` a valid swap canister? Yes (it is registered in SNS-W). No check that `swap_B` is the swap for `P_A`.
5. NNS Governance mints ICP from the Neurons' Fund and sends it to `gov_B` (SNS-B's treasury). SNS-A's Neurons' Fund maturity is consumed. SNS-A's proposal is marked `Committed` and cannot be settled again. [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6980-7038)
```rust
    pub async fn settle_neurons_fund_participation(
        &mut self,
        caller: PrincipalId,
        request: SettleNeuronsFundParticipationRequest,
    ) -> Result<NeuronsFundSnapshot, GovernanceError> {
        let request = ValidatedSettleNeuronsFundParticipationRequest::try_from(request)?;
        let proposal_data = self.get_proposal_data_or_err(
            &request.nns_proposal_id,
            &format!("before awaiting SNS-W for {:?}", request.request_str),
        )?;

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

**File:** rs/nns/governance/src/governance.rs (L7464-7515)
```rust
    /// Asks ICP Ledger to mint `amount_icp_e8s`.
    ///
    /// This function may be called only from `settle_neurons_fund_participation`.
    async fn mint_to_sns_governance(
        &self,
        proposal_id: &ProposalId,
        sns_governance_canister_id: PrincipalId,
        swap_estimated_total_neurons_fund_participation_icp_e8s: u64,
        amount_icp_e8s: u64,
    ) -> Result<(), GovernanceError> {
        // Sanity check if the NNS Governance and the Swap canister agree on how much ICP
        // the Neurons' Fund should participate with.
        //
        // Warning. This value should be used for validation only. NNS Governance should
        // re-compute the amount of Neurons' Fund participation itself. A significant
        // deviation between the self-computed amount and this value would indicates that
        // (1) there is an incompatibility between NNS Governance and Swap, due to a bug or
        // a problematic upgrade or (2) some Neurons' Fund neurons became inactive during
        // the swap.
        if amount_icp_e8s != swap_estimated_total_neurons_fund_participation_icp_e8s {
            println!(
                "{}WARNING: mismatch between amount_icp_e8s computed while settling Neurons' Fund \
                participation in SNS swap created via proposal {:?}. NNS Governance \
                calculation = {}, Swap estimate = {}",
                LOG_PREFIX,
                proposal_id,
                amount_icp_e8s,
                swap_estimated_total_neurons_fund_participation_icp_e8s,
            );
        }

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

        Ok(())
```

**File:** rs/nns/governance/src/governance.rs (L8189-8225)
```rust
/// Given a target_canister_id, is it a CanisterId of a deployed SNS recorded by
/// the SNS-W canister.
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
