Let me verify the exact cross-validation (or lack thereof) between the proposal's stored SNS canister IDs and the caller-supplied `sns_governance_canister_id`.

The vulnerability is confirmed. The code at line 7018 even contains an explicit comment acknowledging the design flaw: `"Note that a Swap could settle each other's participation."` The `is_canister_id_valid_swap_canister_id` check only verifies the caller is *any* registered swap canister, and `mint_to_sns_governance` uses the caller-supplied `sns_governance_canister_id` directly with no cross-validation against the proposal's actual SNS governance canister.

---

### Title
Cross-SNS Neurons' Fund Settlement Allows Arbitrary ICP Minting to Attacker-Controlled Address — (`rs/nns/governance/src/governance.rs`)

### Summary

`settle_neurons_fund_participation` in NNS Governance authorizes any registered SNS swap canister to settle *any* CSNS proposal's Neurons' Fund participation, and mints ICP to a caller-supplied `sns_governance_canister_id` without validating it against the SNS governance canister actually associated with the target proposal. An attacker controlling a legitimate SNS swap canister can supply a victim proposal's `nns_proposal_id` alongside an attacker-controlled `sns_governance_canister_id`, causing NNS Governance to mint the full Neurons' Fund participation amount to an arbitrary address.

### Finding Description

`settle_neurons_fund_participation` performs two independent checks that are never cross-correlated:

**1. Caller authorization** (`rs/nns/governance/src/governance.rs`, lines 7018–7038):

```rust
// Check authorization. Note that a Swap could settle each other's participation.
if let Err(err_msg) =
    is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
```

`is_canister_id_valid_swap_canister_id` (lines 8191–8226) calls `SNS_WASM.list_deployed_snses()` and checks only that the caller's canister ID appears as *any* swap canister in the global list. It does not verify that the caller is the swap canister associated with the specific `nns_proposal_id` in the request. [1](#0-0) 

**2. Mint destination** (`rs/nns/governance/src/governance.rs`, lines 7249–7273):

```rust
} else if let SwapResult::Committed {
    sns_governance_canister_id,
    ...
} = request.swap_result
{
    let mint_icp_result = self
        .mint_to_sns_governance(
            &request.nns_proposal_id,
            sns_governance_canister_id,   // ← taken directly from attacker-controlled request
            ...
        )
        .await;
```

`mint_to_sns_governance` (lines 7467–7515) constructs the ICP ledger destination as `AccountIdentifier::new(sns_governance_canister_id, None)` and mints directly to it. The `sns_governance_canister_id` is never compared against the SNS governance canister ID stored in SNS-W for the proposal being settled. [2](#0-1) 

The code comment at line 7018 explicitly acknowledges the cross-SNS settling is possible ("a Swap could settle each other's participation"), confirming this is a known design gap rather than an oversight in the comment. [3](#0-2) 

The `Committed.sns_governance_canister_id` field in the protobuf is documented as "This is where the minted ICP will be sent" and is fully attacker-controlled. [4](#0-3) 

### Impact Explanation

An attacker controlling SNS swap canister #1 can:

1. Supply `nns_proposal_id` = victim SNS #2's CSNS proposal ID (which has `initial_neurons_fund_participation` set, meaning NF maturity is already reserved).
2. Supply `Committed { sns_governance_canister_id = attacker_wallet, total_direct_participation_icp_e8s = max_direct }`.
3. NNS Governance mints the full computed Neurons' Fund participation amount (potentially millions of ICP) to `attacker_wallet`.
4. The victim proposal's `sns_token_swap_lifecycle` is set to `Committed`, permanently preventing the legitimate swap from ever settling its own Neurons' Fund participation.

The minted amount is bounded by the Neurons' Fund participation computed from `initial_neurons_fund_participation` and the attacker-supplied `total_direct_participation_icp_e8s`. Setting this to the maximum direct participation value maximizes the minted amount. [5](#0-4) 

### Likelihood Explanation

**Preconditions are realistic and achievable on mainnet:**
- (a) At least one legitimate SNS swap must be deployed and registered in SNS-W — this is true today on mainnet.
- (b) A second CSNS proposal must be in Open lifecycle state with Neurons' Fund participation enabled — this is the normal state for any active SNS swap.
- (c) The attacker must control the first SNS's swap canister principal — this requires the attacker to have deployed an SNS (possible via a CSNS proposal) or to have compromised an existing SNS swap canister.

The attacker does not need any privileged NNS access, private keys, or majority corruption. The attack is executable via a single inter-canister call from the attacker's swap canister to NNS Governance. [6](#0-5) 

### Recommendation

In `settle_neurons_fund_participation`, after the caller is verified as a valid swap canister, additionally verify that the caller's canister ID matches the swap canister ID associated with the specific `nns_proposal_id` being settled. This can be done by calling `SNS_WASM.get_deployed_sns_by_proposal_id(nns_proposal_id)` and comparing the returned `swap_canister_id` against `caller`. Similarly, the `sns_governance_canister_id` in the `Committed` message should be validated against the `governance_canister_id` returned by the same SNS-W lookup, rather than being accepted as caller-supplied input. [7](#0-6) 

### Proof of Concept

State-machine test outline:

1. Deploy two SNSes (SNS-1 and SNS-2) via two CSNS proposals, both with Neurons' Fund participation enabled. Both swaps are in `Open` lifecycle. Both proposals have `initial_neurons_fund_participation` set.
2. Attacker (controlling SNS-1's swap canister) calls NNS Governance's `settle_neurons_fund_participation` with:
   - `nns_proposal_id` = proposal ID of SNS-2's CSNS proposal
   - `result = Committed { sns_governance_canister_id = attacker_principal, total_direct_participation_icp_e8s = max_direct_participation }`
3. `is_canister_id_valid_swap_canister_id` passes because SNS-1's swap canister IS in `list_deployed_snses`.
4. No cross-validation occurs between the caller and the proposal's associated SNS.
5. Assert: ICP balance of `attacker_principal` increases by the Neurons' Fund participation amount.
6. Assert: SNS-2's CSNS proposal lifecycle is now `Committed`, blocking the legitimate SNS-2 swap from settling. [8](#0-7)

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

**File:** rs/nns/governance/src/governance.rs (L7198-7218)
```rust
        let direct_participation_icp_e8s = if let SwapResult::Committed {
            total_direct_participation_icp_e8s,
            ..
        } = request.swap_result
        {
            println!(
                "{}INFO: The Swap canister of the SNS created via proposal {:?} has requested \
                Neurons' Fund Matched Funding for {} ICP e8s of direct participation.",
                LOG_PREFIX, request.nns_proposal_id, total_direct_participation_icp_e8s
            );
            total_direct_participation_icp_e8s
        } else {
            println!(
                "{}INFO: The Swap canister of the SNS created via proposal {:?} has reported \
                that the swap had been aborted. There should not be Neurons' Fund participation.",
                LOG_PREFIX, request.nns_proposal_id
            );
            // Our intention is that the following implications hold:
            // Aborted swap ==> Zero direct participation ==> Zero Neurons' Fund participation.
            0
        };
```

**File:** rs/nns/governance/src/governance.rs (L7266-7273)
```rust
            let mint_icp_result = self
                .mint_to_sns_governance(
                    &request.nns_proposal_id,
                    sns_governance_canister_id,
                    swap_estimated_total_neurons_fund_participation_icp_e8s,
                    amount_icp_e8s,
                )
                .await;
```

**File:** rs/nns/governance/src/governance.rs (L7495-7506)
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
```

**File:** rs/nns/governance/src/governance.rs (L8215-8223)
```rust
    let is_swap = list_deployed_snses_response
        .instances
        .iter()
        .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
    if !is_swap {
        return Err(format!(
            "target_swap_canister_id is not the ID of any swap canister known to sns_wasm: {target_canister_id}"
        ));
    }
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
