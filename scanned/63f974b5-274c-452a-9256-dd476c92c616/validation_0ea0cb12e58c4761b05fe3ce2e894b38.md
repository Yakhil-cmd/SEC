### Title
Any Deployed SNS Swap Canister Can Settle Another Proposal's Neurons' Fund Participation, Redirecting Minted ICP - (`File: rs/nns/governance/src/governance.rs`)

---

### Summary

`settle_neurons_fund_participation` in NNS Governance authorizes the caller only as *any* valid SNS swap canister, not as the *specific* swap canister associated with the target proposal. A malicious or compromised SNS swap canister can call this function with a *different* proposal's ID, overwrite that proposal's lifecycle state, and redirect minted ICP (drawn from Neurons' Fund maturity) to an attacker-controlled canister.

---

### Finding Description

The function `settle_neurons_fund_participation` in `rs/nns/governance/src/governance.rs` performs the following authorization check:

```rust
// Check authorization. Note that a Swap could settle each other's participation.
let target_canister_id: CanisterId = caller.try_into()...?;
if let Err(err_msg) =
    is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
{
    return Err(...NotAuthorized...);
}
``` [1](#0-0) 

`is_canister_id_valid_swap_canister_id` queries SNS-W's `list_deployed_snses` and checks whether the caller is *any* deployed SNS swap canister:

```rust
let is_swap = list_deployed_snses_response
    .instances
    .iter()
    .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
``` [2](#0-1) 

There is **no check** that the caller is the swap canister *associated with the specific proposal* identified by `nns_proposal_id`. The proposal's deployed SNS record (stored in SNS-W, keyed by proposal ID via `get_deployed_sns_by_proposal_id`) contains the correct `swap_canister_id`, but this is never cross-referenced against `caller` inside `settle_neurons_fund_participation`. [3](#0-2) 

When the request is `Committed`, the function mints ICP to `sns_governance_canister_id` taken directly from the attacker-supplied request body, not from the proposal's actual SNS governance canister:

```rust
let mint_icp_result = self
    .mint_to_sns_governance(
        &request.nns_proposal_id,
        sns_governance_canister_id,   // ← from attacker-controlled request
        ...
    )
    .await;
``` [4](#0-3) 

The comment at line 7018 explicitly acknowledges the design gap: *"Note that a Swap could settle each other's participation."* [5](#0-4) 

---

### Impact Explanation

A malicious SNS swap canister (Swap B, deployed for Proposal 2) can call `settle_neurons_fund_participation` targeting Proposal 1 (a different, legitimate SNS):

1. **Unauthorized ICP minting**: By supplying `result = Committed { sns_governance_canister_id = attacker_canister }`, Swap B causes NNS Governance to mint ICP (drawn from Neurons' Fund maturity reserved for Proposal 1) and send it to an attacker-controlled canister. This is a direct ledger conservation violation — ICP is minted without the legitimate SNS swap having committed.

2. **Proposal lifecycle state overwrite**: The call sets `sns_token_swap_lifecycle` of Proposal 1 to `Committed` or `Aborted`, permanently locking out the legitimate Swap A from ever settling. Once the lifecycle is set, the lock prevents retries by the correct swap canister.

3. **Neurons' Fund maturity loss**: Neurons' Fund neurons have their reserved maturity burned/converted to ICP that is sent to the attacker, with no corresponding SNS neurons created for Neurons' Fund participants. [6](#0-5) 

---

### Likelihood Explanation

- **Attacker-controlled entry path**: Any deployed SNS swap canister can send an inter-canister call to NNS Governance's `settle_neurons_fund_participation`. SNS swap canisters are deployed by SNS-W but are subsequently controlled by their own SNS governance, which can be manipulated via SNS proposals. A malicious SNS project can deploy a legitimate-looking SNS, gain swap canister status, and then exploit this.
- **No privileged access required**: The attacker only needs to be a registered SNS swap canister — a status achievable by any project that passes an NNS `CreateServiceNervousSystem` proposal.
- **Timing window**: The attack must occur before the legitimate swap canister calls `settle_neurons_fund_participation`. Since finalization is not automatic (it requires an explicit call), there is a window between swap conclusion and finalization.
- **Multiple SNS instances**: As the IC ecosystem grows and more SNS instances are deployed, the pool of potential attacker-controlled swap canisters grows. [7](#0-6) 

---

### Recommendation

1. **Bind the caller to the proposal**: After retrieving `proposal_data` for `nns_proposal_id`, look up the deployed SNS for that proposal via SNS-W's `get_deployed_sns_by_proposal_id` and assert that `caller == deployed_sns.swap_canister_id`. This ensures only the specific swap canister for that proposal can settle it.

2. **Validate `sns_governance_canister_id` against the proposal**: When the request is `Committed`, cross-check the `sns_governance_canister_id` field in the request against the SNS governance canister recorded for the proposal in SNS-W, rather than blindly minting to the caller-supplied value.

3. **Remove the acknowledged design gap**: The comment *"Note that a Swap could settle each other's participation"* should be treated as a known bug, not an accepted design. The fix in recommendation (1) closes this gap.

---

### Proof of Concept

**Setup**: Two SNS instances exist — SNS-A (Proposal 1, legitimate) and SNS-B (Proposal 2, attacker-controlled). SNS-B's swap canister is controlled by a malicious actor.

**Attack**:

```
// Attacker calls from SNS-B's swap canister:
nns_governance.settle_neurons_fund_participation(
    SettleNeuronsFundParticipationRequest {
        nns_proposal_id: Some(proposal_1_id),  // ← Proposal 1 (SNS-A's proposal)
        result: Some(Result::Committed(Committed {
            sns_governance_canister_id: Some(attacker_canister_id), // ← attacker's canister
            total_direct_participation_icp_e8s: Some(max_icp_e8s), // ← maximize NF contribution
            total_neurons_fund_participation_icp_e8s: Some(0),
        })),
    }
)
```

**Authorization check** (line 7028–7038): Passes, because SNS-B's swap canister is a valid SNS swap canister per `list_deployed_snses`.

**Result**:
- NNS Governance mints ICP from Neurons' Fund maturity reserved for Proposal 1 and sends it to `attacker_canister_id`.
- `proposal_1.sns_token_swap_lifecycle` is set to `Committed`, blocking SNS-A's legitimate swap from ever settling.
- SNS-A's Neurons' Fund participants receive no SNS neurons despite their maturity being consumed. [8](#0-7) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6980-7045)
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
        // Re-acquire the proposal_data mutably after `SnsWasm.list_deployed_snses().await`.
        // Mutability will be needed later, when we aquire the lock (see
        // `proposal_data.set_swap_lifecycle_by_settle_neurons_fund_participation_request_type`).
        let proposal_data = self.mut_proposal_data_or_err(
            &request.nns_proposal_id,
            &format!("after awaiting SNS-W for {:?}", request.request_str),
        )?;
```

**File:** rs/nns/governance/src/governance.rs (L7234-7237)
```rust
        // Set the lifecycle of the proposal to avoid interleaving callers.
        proposal_data.set_swap_lifecycle_by_settle_neurons_fund_participation_request_type(
            &request.swap_result,
        );
```

**File:** rs/nns/governance/src/governance.rs (L7249-7277)
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

            // We need to clone the snapshot because `final_neurons_fund_participation` is recorded
            // in stable memory, while the snapshot is used to build up this function's response.
            mint_icp_result.map(|_| final_neurons_fund_participation.snapshot_cloned())
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
