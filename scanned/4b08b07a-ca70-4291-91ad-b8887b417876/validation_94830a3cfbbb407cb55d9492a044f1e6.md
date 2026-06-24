### Title
Any SNS Swap Canister Can Settle Another SNS's Neurons' Fund Participation - (`rs/nns/governance/src/governance.rs`)

### Summary

The `settle_neurons_fund_participation` function in NNS Governance authorizes callers by checking whether the caller is *any* valid SNS swap canister (via `is_canister_id_valid_swap_canister_id`), rather than verifying the caller is specifically the swap canister associated with the `nns_proposal_id` supplied in the request. This mirrors the Popcorn adapter-pause bug: just as any vault creator sharing an adapter could pause it, any deployed SNS swap canister can settle the Neurons' Fund participation for any other SNS's proposal, including with a fabricated result.

### Finding Description

`settle_neurons_fund_participation` in `rs/nns/governance/src/governance.rs` performs two independent checks:

1. It verifies the `nns_proposal_id` in the request corresponds to a `CreateServiceNervousSystem` proposal.
2. It verifies the caller is a valid swap canister ID by querying `list_deployed_snses` on SNS-W.

The code itself acknowledges the gap with an inline comment:

```rust
// Check authorization. Note that a Swap could settle each other's participation.
let target_canister_id: CanisterId = caller.try_into()...
if let Err(err_msg) =
    is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
``` [1](#0-0) 

`is_canister_id_valid_swap_canister_id` checks only whether the caller appears in the global list of deployed SNS swap canisters — it does not verify that the caller is the swap canister *for the specific proposal being settled*:

```rust
let is_swap = list_deployed_snses_response
    .instances
    .iter()
    .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
``` [2](#0-1) 

The `nns_proposal_id` field in `SettleNeuronsFundParticipationRequest` is caller-supplied and is never cross-checked against the swap canister ID that was actually deployed for that proposal: [3](#0-2) 

In the `Committed` path, the `sns_governance_canister_id` destination for ICP minting also comes entirely from the caller's request, not from the on-chain proposal data: [4](#0-3) 

Once settled, the function is idempotent — subsequent calls return the previously stored result — so the first caller wins and the legitimate swap canister cannot undo the settlement: [5](#0-4) 

### Impact Explanation

**Attack path 1 — Aborted DoS:** SNS Swap A calls `settle_neurons_fund_participation` with SNS B's `nns_proposal_id` and `result = Aborted`. NNS Governance refunds all reserved Neurons' Fund maturity back to NNS neurons. When SNS B's legitimate swap later commits and calls the same function, it receives the already-stored empty snapshot and no ICP is ever minted to SNS B's treasury. SNS B's Neurons' Fund participants receive no SNS neurons.

**Attack path 2 — ICP redirection:** SNS Swap A calls with SNS B's `nns_proposal_id`, `result = Committed`, and `sns_governance_canister_id = <attacker-controlled address>`. NNS Governance computes the correct ICP amount from SNS B's proposal data and mints it to the attacker-controlled address. SNS B's treasury receives nothing.

Both attacks permanently lock the proposal's lifecycle to a terminal state, preventing any retry by the legitimate swap. [6](#0-5) 

### Likelihood Explanation

Every deployed SNS swap canister is a valid caller. SNS swap canisters are deployed by SNS-W and upgraded via NNS proposals, so they are nominally trusted. However:

- A bug in any SNS swap canister's `finalize_swap` logic could cause it to call `settle_neurons_fund_participation` with an incorrect `nns_proposal_id`.
- A malicious SNS proposal (passed by governance) could install swap canister code that deliberately targets other SNS proposals.
- The attack window is the period between SNS B's swap opening and its legitimate finalization call — a window that can last days or weeks.

The code comment explicitly acknowledges the design flaw, indicating the developers are aware but have not mitigated it. [7](#0-6) 

### Recommendation

After the `is_canister_id_valid_swap_canister_id` check passes, additionally verify that the caller's canister ID matches the swap canister ID recorded for the specific `nns_proposal_id` in the proposal data. The `list_deployed_snses` response already contains the `swap_canister_id` per SNS instance alongside the `nns_proposal_id` that created it; use that binding to enforce the pairing:

```rust
// Pseudocode
let expected_swap_id = list_deployed_snses_response
    .instances
    .iter()
    .find(|sns| sns.nns_proposal_id == Some(request.nns_proposal_id))
    .and_then(|sns| sns.swap_canister_id);

if expected_swap_id != Some(caller_canister_id.into()) {
    return Err(NotAuthorized(...));
}
```

Additionally, the `sns_governance_canister_id` in the `Committed` case should be validated against the value stored in the proposal data rather than trusted from the caller's request.

### Proof of Concept

1. Two SNS instances exist: SNS-A (swap canister `swap_a`) and SNS-B (swap canister `swap_b`, proposal ID `proposal_b`). Both are listed by `list_deployed_snses` on SNS-W.
2. SNS-B's swap is open; its Neurons' Fund participation has been reserved (`initial_neurons_fund_participation` is set in `proposal_b`'s `neurons_fund_data`).
3. `swap_a` calls `settle_neurons_fund_participation` on NNS Governance with `{ nns_proposal_id: proposal_b, result: Aborted {} }`.
4. `is_canister_id_valid_swap_canister_id` returns `Ok` because `swap_a` is a valid swap canister.
5. NNS Governance settles `proposal_b` as `Aborted`, refunding all reserved maturity to NNS neurons and setting `proposal_b.sns_token_swap_lifecycle = Aborted`.
6. When `swap_b` later commits and calls `settle_neurons_fund_participation` with `{ nns_proposal_id: proposal_b, result: Committed { sns_governance_canister_id: sns_b_gov, ... } }`, it hits the idempotency branch and receives an empty `NeuronsFundSnapshot` — no ICP is minted to SNS-B's treasury. [8](#0-7) [5](#0-4)

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
