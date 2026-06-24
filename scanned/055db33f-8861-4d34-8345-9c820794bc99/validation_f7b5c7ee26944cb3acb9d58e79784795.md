### Title
Arbitrary Caller-Provided `sns_governance_canister_id` Used as ICP Mint Destination Without Validation Against Stored Proposal State - (File: rs/nns/governance/src/governance.rs)

---

### Summary

In `settle_neurons_fund_participation` within the NNS Governance canister, the authorization check only verifies that the caller is *any* valid SNS Swap canister, but never validates that the caller is the *specific* Swap canister associated with the `nns_proposal_id` in the request. Additionally, the `sns_governance_canister_id` field — which determines where minted ICP is sent — is taken directly from the caller-provided request payload rather than from the stored proposal data. Any deployed SNS Swap canister can therefore settle another SNS's Neurons' Fund participation and redirect the minted ICP to an arbitrary destination.

---

### Finding Description

The public update method `settle_neurons_fund_participation` in `rs/nns/governance/canister/canister.rs` is callable by any canister: [1](#0-0) 

It delegates to `Governance::settle_neurons_fund_participation` in `rs/nns/governance/src/governance.rs`: [2](#0-1) 

The authorization check at lines 7018–7038 only verifies that the caller is *some* valid SNS Swap canister registered with SNS-W — it does **not** verify that the caller is the Swap canister specifically associated with the `nns_proposal_id` supplied in the request. The code comment even acknowledges this gap explicitly: [3](#0-2) 

After passing this check, the function proceeds to mint ICP using the `sns_governance_canister_id` extracted directly from the caller-provided `request.swap_result`, not from the stored `ProposalData`: [4](#0-3) 

The `mint_to_sns_governance` function then transfers ICP to whatever `PrincipalId` was supplied in the request: [5](#0-4) 

The `SettleNeuronsFundParticipationRequest::Committed` struct, as defined in the protobuf and Rust types, exposes `sns_governance_canister_id` as a fully caller-controlled field: [6](#0-5) 

There is no code path that reads the `sns_governance_canister_id` from the stored `ProposalData` and compares it against the caller-supplied value before minting.

---

### Impact Explanation

A malicious SNS Swap canister (SNS-B's swap) can call `settle_neurons_fund_participation` on NNS Governance supplying:
- `nns_proposal_id` = the proposal ID of a legitimate SNS-A that has Neurons' Fund participation reserved
- `result.Committed.sns_governance_canister_id` = an attacker-controlled canister principal

NNS Governance will:
1. Confirm the proposal is a `CreateServiceNervousSystem` proposal (it is — SNS-A's)
2. Confirm the caller is a valid SNS Swap canister (it is — SNS-B's)
3. Compute the Neurons' Fund ICP amount for SNS-A's proposal
4. Mint that ICP and transfer it to the attacker-controlled canister instead of SNS-A's governance canister
5. Mark SNS-A's proposal lifecycle as `Committed`, permanently preventing legitimate settlement

This constitutes:
- **Theft of ICP** minted from the Neurons' Fund (maturity converted to ICP and redirected)
- **Permanent denial of service** for SNS-A's legitimate swap settlement (the lifecycle is set to terminal, blocking retries)
- **Ledger conservation violation**: ICP is minted to an unintended recipient

---

### Likelihood Explanation

The attacker must control a valid SNS Swap canister registered with SNS-W. This requires having successfully deployed an SNS through the NNS governance process — a realistic condition given the number of active SNS deployments on mainnet. Once an attacker controls any SNS Swap canister, they can target any other SNS that has Neurons' Fund participation enabled and whose settlement has not yet been finalized. The window of opportunity exists between swap commitment and the legitimate `finalize_swap` call completing `settle_neurons_fund_participation`.

---

### Recommendation

1. **Short term**: After the `is_canister_id_valid_swap_canister_id` check passes, retrieve the `sns_governance_canister_id` and the associated Swap canister ID from the stored `ProposalData` (via the `CreateServiceNervousSystem` action's deployed SNS record) and assert that `caller == stored_swap_canister_id` and `request.sns_governance_canister_id == stored_sns_governance_canister_id`. Do not use any caller-supplied field as the ICP mint destination.

2. **Long term**: Derive all settlement parameters (destination canister, lifecycle transition) exclusively from on-chain `ProposalData`, treating the caller-provided `SettleNeuronsFundParticipationRequest` fields as advisory/informational only (e.g., for the `total_direct_participation_icp_e8s` sanity check that already exists at line 7483).

---

### Proof of Concept

1. Attacker deploys SNS-B through NNS governance, obtaining a valid SNS-B Swap canister ID registered with SNS-W.
2. SNS-A is a legitimate SNS with Neurons' Fund participation enabled, in `Committed` lifecycle, not yet finalized.
3. Attacker's SNS-B Swap canister calls NNS Governance `settle_neurons_fund_participation` with:
   ```
   nns_proposal_id = <SNS-A's CSS proposal ID>
   result = Committed {
       sns_governance_canister_id = <attacker-controlled canister>,
       total_direct_participation_icp_e8s = <any value>,
       total_neurons_fund_participation_icp_e8s = <any value>,
   }
   ```
4. NNS Governance passes the `is_canister_id_valid_swap_canister_id` check (SNS-B's swap is valid).
5. NNS Governance computes the Neurons' Fund ICP for SNS-A's proposal and calls `mint_to_sns_governance` with the attacker-supplied `sns_governance_canister_id`.
6. ICP is minted to the attacker-controlled canister; SNS-A's proposal lifecycle is set to `Committed`, blocking any future legitimate settlement. [7](#0-6) [8](#0-7) [9](#0-8)

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
