### Title
Any Deployed SNS Swap Canister Can Settle Another Swap's Neurons' Fund Participation With a False Result - (File: rs/nns/governance/src/governance.rs)

### Summary
`settle_neurons_fund_participation` in NNS governance only verifies that the caller is *any* deployed SNS swap canister, not the *specific* swap canister associated with the target proposal. This mirrors the flashloan callback vulnerability: just as the Zapper only checked `msg.sender == flashLoanProvider` without verifying the original initiator, NNS governance only checks "is the caller a valid swap canister?" without checking "is the caller the swap canister for *this* proposal?" Any deployed SNS swap canister can therefore call `settle_neurons_fund_participation` with a victim swap's `nns_proposal_id` and a falsified `Aborted` result, permanently poisoning the victim swap's Neurons' Fund settlement.

### Finding Description
In `rs/nns/governance/src/governance.rs`, the authorization check for `settle_neurons_fund_participation` is:

```rust
// Check authorization. Note that a Swap could settle each other's participation.
let target_canister_id: CanisterId = caller.try_into()...?;
if let Err(err_msg) =
    is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
{
    return Err(...NotAuthorized...);
}
``` [1](#0-0) 

`is_canister_id_valid_swap_canister_id` queries SNS-W's `list_deployed_snses` and checks whether the caller appears as *any* swap canister in the global list:

```rust
let is_swap = list_deployed_snses_response
    .instances
    .iter()
    .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
``` [2](#0-1) 

There is no check that `caller == proposal_data.swap_canister_id`. The `nns_proposal_id` and the `result` (Committed/Aborted) are both fully attacker-controlled fields in the request. The code's own comment acknowledges this: *"Note that a Swap could settle each other's participation."* [3](#0-2) 

### Impact Explanation
An attacker who controls a legitimately deployed SNS swap canister (Swap A) can:

1. Call `settle_neurons_fund_participation` on NNS governance with a victim SNS's `nns_proposal_id` and `result = Aborted`.
2. NNS governance accepts the call because Swap A is a valid swap canister.
3. NNS governance refunds all Neurons' Fund maturity reserved for the victim proposal and sets the proposal's `sns_token_swap_lifecycle` to `Aborted`.
4. When the victim's own swap canister (Swap B) later calls `settle_neurons_fund_participation` with `result = Committed` during `finalize`, the lifecycle is already set and the call fails or is a no-op.
5. Swap B never receives the Neurons' Fund ICP it was entitled to, causing the SNS swap finalization to fail or produce an inconsistent state where SNS tokens are distributed but no matching ICP arrives from the Neurons' Fund.

The Neurons' Fund participants have their maturity refunded (not a loss for them), but the SNS project loses the Neurons' Fund ICP contribution, and the swap's `finalize` flow is broken. [4](#0-3) 

### Likelihood Explanation
The attacker must first deploy a legitimate SNS through the NNS governance proposal process, which requires community approval. This is a high barrier. However, once an SNS is deployed, its swap canister is a persistent on-chain actor that can call `settle_neurons_fund_participation` for any proposal at any time before the victim swap's own `finalize` call completes. The attack window is the period between a victim swap reaching `Committed` or `Aborted` state and the victim swap canister executing `finalize`. Since `finalize` is not automatic (per the swap proto comments), this window can be non-trivial. [5](#0-4) 

### Recommendation
Bind the authorization to the specific proposal: store the swap canister ID in the `ProposalData` at proposal creation time (it is already known when the SNS is deployed), and require `caller == proposal_data.derived_swap_canister_id`. The check should be:

```rust
// Proposed fix
if caller != proposal_data.swap_canister_id {
    return Err(GovernanceError::new_with_message(
        ErrorType::NotAuthorized,
        format!("Only the swap canister for this proposal may settle its Neurons' Fund participation."),
    ));
}
```

This is analogous to the mitigation suggested in the original report: generate a per-operation ID and verify the callback was started by the correct initiator.

### Proof of Concept
1. Attacker submits and passes an NNS governance proposal to create SNS-A. SNS-A's swap canister (`swap_a`) is deployed.
2. Victim SNS-B is in `Committed` state with `nns_proposal_id = 42` and has Neurons' Fund participation reserved.
3. Before SNS-B's swap canister calls `finalize`, attacker calls from `swap_a`:
   ```
   settle_neurons_fund_participation({
       nns_proposal_id: 42,
       result: Aborted { }
   })
   ```
4. NNS governance checks: is `swap_a` in `list_deployed_snses`? Yes → authorized.
5. Neurons' Fund maturity for proposal 42 is refunded; lifecycle set to `Aborted`.
6. SNS-B's swap canister calls `finalize` → `settle_neurons_fund_participation` with `Committed` → fails because lifecycle is already `Aborted`.
7. SNS-B's finalization is broken; it never receives Neurons' Fund ICP. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6980-6984)
```rust
    pub async fn settle_neurons_fund_participation(
        &mut self,
        caller: PrincipalId,
        request: SettleNeuronsFundParticipationRequest,
    ) -> Result<NeuronsFundSnapshot, GovernanceError> {
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L136-140)
```text
// the swap. In this state, a call to `finalize` will create SNS
// neurons for each participant and transfer ICP to the SNS governance
// canister. The call to `finalize` does not happen automatically
// (i.e., on the canister heartbeat) so that there is a caller to
// respond to with potential errors.
```

**File:** rs/sns/swap/src/swap.rs (L1563-1568)
```rust
        // Settle the Neurons' Fund participation in the token swap.
        finalize_swap_response.set_settle_neurons_fund_participation_result(
            self.settle_neurons_fund_participation(environment.nns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
```
