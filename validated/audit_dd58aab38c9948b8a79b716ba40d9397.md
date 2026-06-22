### Title
Any Valid SNS Swap Canister Can Settle Another SNS's Neurons' Fund Participation and Redirect Minted ICP — (`rs/nns/governance/src/governance.rs`)

---

### Summary

`settle_neurons_fund_participation` in NNS Governance authorizes any legitimately deployed SNS swap canister to settle the Neurons' Fund participation for **any** proposal, not just its own. Because the caller-supplied `sns_governance_canister_id` (the ICP mint destination) is never validated against the proposal's stored data, a malicious SNS swap canister can redirect Neurons' Fund ICP to an attacker-controlled address and permanently corrupt the victim SNS's settlement state.

---

### Finding Description

`Governance::settle_neurons_fund_participation` performs two authorization steps:

1. It verifies the caller is a valid CanisterId.
2. It calls `is_canister_id_valid_swap_canister_id`, which queries SNS-W's `list_deployed_snses` to confirm the caller is *any* registered SNS swap canister. [1](#0-0) 

The comment at line 7018 explicitly acknowledges the gap: *"Note that a Swap could settle each other's participation."*

After passing this check, the function reads the `nns_proposal_id` from the request to look up the victim proposal's Neurons' Fund data, then uses the caller-supplied `sns_governance_canister_id` field from the `Committed` variant as the ICP mint destination: [2](#0-1) 

`mint_to_sns_governance` constructs the ledger destination directly from this caller-controlled field: [3](#0-2) 

There is no check that `sns_governance_canister_id` matches the SNS governance canister recorded in the proposal data for `nns_proposal_id`. The `SettleNeuronsFundParticipationRequest::Committed` struct exposes this field as a free parameter: [4](#0-3) 

After minting, the function permanently marks the proposal's lifecycle as `Committed` or `Aborted`, preventing any future legitimate settlement: [5](#0-4) 

---

### Impact Explanation

A malicious actor who controls a legitimately deployed SNS swap canister can:

1. **Steal Neurons' Fund ICP**: By supplying a victim SNS's `nns_proposal_id` and an attacker-controlled `sns_governance_canister_id`, the attacker causes NNS Governance to mint and transfer the full Neurons' Fund participation amount (potentially millions of ICP e8s) to the attacker's address.
2. **Permanently corrupt the victim SNS**: The proposal's lifecycle is set to `Committed` and `final_neurons_fund_participation` is recorded. The legitimate swap canister for that SNS can never re-settle; its subsequent call will hit the idempotency guard at line 7166 and return the attacker's fraudulent data.
3. **Drain Neurons' Fund maturity**: The Neurons' Fund neurons have their reserved maturity consumed without receiving the corresponding SNS tokens, causing permanent loss to NNS neuron holders. [6](#0-5) 

---

### Likelihood Explanation

Deploying a legitimate SNS requires submitting a `CreateServiceNervousSystem` NNS proposal that passes governance voting. This is a real but reachable barrier — any sufficiently motivated actor with ICP stake can do it. Once the attacker's SNS is deployed and its swap canister is registered in SNS-W, the attack requires a single cross-canister call to NNS Governance with a crafted `SettleNeuronsFundParticipationRequest`. The window of opportunity is any time between a victim SNS swap opening and its legitimate settlement call. No privileged keys, no threshold corruption, and no social engineering are required beyond the normal SNS deployment process.

---

### Recommendation

1. **Bind the caller to the proposal**: After the `is_canister_id_valid_swap_canister_id` check, additionally verify that the caller's canister ID matches the swap canister ID recorded in the SNS-W deployment entry for the `nns_proposal_id`'s associated SNS instance. The `list_deployed_snses` response already contains both `swap_canister_id` and `governance_canister_id` per instance — use them.

2. **Derive `sns_governance_canister_id` from proposal data**: Do not trust the caller-supplied `sns_governance_canister_id`. Instead, look up the SNS governance canister ID from the proposal's stored `CreateServiceNervousSystem` action or from the SNS-W deployment record, and use that as the mint destination.

3. **Remove the cross-swap settlement allowance**: The comment "a Swap could settle each other's participation" describes the current (broken) behavior, not an intended feature. The check should be `caller == swap_canister_id_for_this_proposal`.

---

### Proof of Concept

```
Attacker deploys SNS-A (legitimate, registered in SNS-W)
  → Attacker controls SNS-A's swap canister (swap_A)

Victim SNS-B exists with:
  → nns_proposal_id = P_B
  → Neurons' Fund participation reserved (initial_neurons_fund_participation set)
  → Swap not yet settled (final_neurons_fund_participation = None)

Attack call (from swap_A):
  ic_cdk::call(
    NNS_GOVERNANCE_CANISTER_ID,
    "settle_neurons_fund_participation",
    SettleNeuronsFundParticipationRequest {
      nns_proposal_id: Some(P_B),          // victim's proposal
      result: Some(Committed {
        sns_governance_canister_id: Some(ATTACKER_PRINCIPAL),  // attacker's address
        total_direct_participation_icp_e8s: Some(u64::MAX),    // maximize NF participation
        total_neurons_fund_participation_icp_e8s: Some(0),
      }),
    }
  )

NNS Governance:
  1. is_canister_id_valid_swap_canister_id(swap_A) → Ok  (swap_A is a real swap)
  2. Looks up proposal P_B → finds NF participation data
  3. Computes final_neurons_fund_participation for P_B
  4. Calls mint_to_sns_governance(P_B, ATTACKER_PRINCIPAL, ...)
     → ICP Ledger mints NF ICP to ATTACKER_PRINCIPAL
  5. Sets P_B lifecycle = Committed, records final participation
  6. Returns success to swap_A

Result:
  - Attacker receives Neurons' Fund ICP meant for SNS-B
  - SNS-B's proposal is permanently marked Committed with wrong data
  - SNS-B's legitimate swap can never re-settle (idempotency guard returns attacker's data)
```

The root cause is in `rs/nns/governance/src/governance.rs` at `settle_neurons_fund_participation` (lines 6980–7038): the authorization check confirms the caller is *a* swap canister but not *the* swap canister for the supplied `nns_proposal_id`, and the mint destination at lines 7266–7272 is taken from the attacker-controlled request field rather than from the proposal's stored SNS governance canister ID. [7](#0-6) [1](#0-0) [8](#0-7) [4](#0-3) [9](#0-8)

### Citations

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

**File:** rs/nns/governance/src/governance.rs (L7464-7516)
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
    }
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
