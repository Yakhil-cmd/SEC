### Title
Any SNS Swap Canister Can Settle Neurons' Fund Participation for Any Other SNS Proposal - (File: `rs/nns/governance/src/governance.rs`)

### Summary
The `settle_neurons_fund_participation` function in NNS governance verifies only that the caller is *any* valid SNS swap canister, but never checks that the caller is the *specific* swap canister associated with the proposal being settled. Any deployed SNS swap canister can therefore call this function with another SNS's `nns_proposal_id`, forcing an `Aborted` settlement and permanently blocking the legitimate swap from receiving its Neurons' Fund ICP contribution.

### Finding Description
`settle_neurons_fund_participation` in `rs/nns/governance/src/governance.rs` performs two authorization steps:

1. It confirms the proposal action is `CreateServiceNervousSystem`.
2. It calls `is_canister_id_valid_swap_canister_id`, which queries SNS-W for the full list of all deployed SNS instances and checks whether the caller appears as *any* swap canister in that list. [1](#0-0) 

The comment at line 7018 explicitly acknowledges the gap: *"Note that a Swap could settle each other's participation."* No check is ever made that `caller == proposal.swap_canister_id`.

`is_canister_id_valid_swap_canister_id` iterates over all deployed SNS instances and returns `Ok` if the caller matches any of them: [2](#0-1) 

This is structurally identical to the reported Solidity bug: the gateway checks `isAccountAuthorized(msg.sender)` (caller is *a* shareholder) but never checks `msg.sender == account` (caller is *this* shareholder). Here, governance checks "caller is *a* swap canister" but never checks "caller is *the* swap canister for this proposal."

The older `SettleCommunityFundParticipation` message explicitly required the opposite: *"The caller's principal ID must match the value in the `target_swap_canister_id` field in the proposal."* [3](#0-2) 

The new `SettleNeuronsFundParticipationRequest` dropped that binding entirely. [4](#0-3) 

### Impact Explanation
A malicious SNS swap canister (Swap A) can call `settle_neurons_fund_participation` targeting another SNS's `nns_proposal_id` (Swap B) with `result = Aborted` before Swap B's own swap canister does. The function is idempotent: once settled, subsequent calls return the previously stored result. [5](#0-4) 

Consequences for Swap B:
- The Neurons' Fund maturity reserved for Swap B is immediately refunded to NF neurons.
- The proposal lifecycle is permanently set to `Aborted`.
- When Swap B's legitimate swap canister later calls `settle_neurons_fund_participation` with `Committed`, it receives the already-stored `Aborted` result and cannot override it.
- Swap B's SNS treasury never receives the Neurons' Fund ICP contribution — a direct financial loss for all of Swap B's participants.

Additionally, in the `Committed` path the `sns_governance_canister_id` destination is taken directly from the caller-supplied request and is not validated against the proposal's stored SNS canister IDs, meaning a malicious swap canister could also redirect minted ICP to an address it controls.

### Likelihood Explanation
The attacker must first deploy a legitimate SNS through NNS governance (requiring a passed NNS proposal). Once that threshold is crossed, the attacker holds a valid swap canister ID and can target any other SNS's Neurons' Fund settlement with a single update call. The window of vulnerability is the period between a target SNS's swap ending and its swap canister calling `settle_neurons_fund_participation`. Because `finalize_swap` is triggered externally and can be delayed, this window can be hours to days. The attack requires no privileged key, no social engineering, and no consensus-level corruption.

### Recommendation
- **Short term:** Inside `settle_neurons_fund_participation`, after retrieving `proposal_data`, extract the swap canister ID stored in the proposal's SNS deployment record and assert `caller == stored_swap_canister_id`. Reject the call with `NotAuthorized` if they differ.
- **Long term:** Store the deployed swap canister ID on `ProposalData` at SNS deployment time (analogous to how `OpenSnsTokenSwap` stored `target_swap_canister_id`). Add negative-path integration tests asserting that a different valid swap canister cannot settle another SNS's Neurons' Fund participation.

### Proof of Concept
1. Attacker deploys SNS A via NNS governance; SNS A's swap canister ID is now registered in SNS-W.
2. SNS B completes its swap in `Committed` state; its `finalize_swap` has not yet been called.
3. Attacker's SNS A swap canister calls NNS governance `settle_neurons_fund_participation` with:
   - `nns_proposal_id` = Swap B's NNS proposal ID
   - `result = Aborted`
4. Governance confirms SNS A's swap canister is a valid swap canister — check passes.
5. Governance settles Swap B's Neurons' Fund participation as `Aborted`, refunds NF maturity, and stores the result.
6. Swap B's legitimate swap canister later calls `settle_neurons_fund_participation` with `Committed`; governance returns the already-stored `Aborted` result (idempotency path, line 7166–7174).
7. Swap B's SNS treasury receives zero Neurons' Fund ICP; all NF maturity has already been refunded. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L6980-6990)
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L1058-1062)
```text
message SettleCommunityFundParticipation {
  // The caller's principal ID must match the value in the
  // target_swap_canister_id field in the proposal (more precisely, in the
  // OpenSnsTokenSwap).
  optional uint64 open_sns_token_swap_proposal_id = 1;
```
