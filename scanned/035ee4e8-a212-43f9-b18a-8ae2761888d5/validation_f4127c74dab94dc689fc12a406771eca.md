### Title
Any Deployed SNS Swap Canister Can Settle Another SNS's Neurons' Fund Participation — (`rs/nns/governance/src/governance.rs`)

### Summary

The `settle_neurons_fund_participation` endpoint on NNS Governance verifies only that the caller is *some* valid SNS Swap canister, not that it is the specific Swap canister associated with the `nns_proposal_id` supplied in the request. The code itself acknowledges this gap with the comment: *"Note that a Swap could settle each other's participation."* Any deployed SNS Swap canister can therefore call this endpoint with an arbitrary `nns_proposal_id`, triggering settlement of a completely different SNS's Neurons' Fund participation.

### Finding Description

`settle_neurons_fund_participation` is a public `#[update]` endpoint on the NNS Governance canister. [1](#0-0) 

Inside the implementation, the authorization logic converts the caller to a `CanisterId` and then calls `is_canister_id_valid_swap_canister_id`, which queries SNS-W to confirm the caller is *any* deployed SNS Swap canister. It does **not** cross-check that the caller is the Swap canister that was created by the specific `nns_proposal_id` supplied in the request body. [2](#0-1) 

The comment at line 7018 explicitly acknowledges the gap: *"Check authorization. Note that a Swap could settle each other's participation."*

After the authorization check passes, the function fetches proposal data keyed by the caller-supplied `nns_proposal_id`, sets the proposal lifecycle to `Committed` or `Aborted`, and — in the `Committed` branch — mints ICP to the `sns_governance_canister_id` field that is also caller-supplied: [3](#0-2) 

Because both `nns_proposal_id` and `sns_governance_canister_id` come from the attacker-controlled request, a malicious Swap canister can:

1. Target any other SNS's `nns_proposal_id`.
2. Supply an attacker-controlled `sns_governance_canister_id` as the ICP mint destination.
3. Declare the swap `Committed` (even if it is still open or aborted) or `Aborted` (even if it committed successfully).

The design rationale recorded in the proto comment is that all SNS Swap canisters are trusted because they are deployed by SNS-W: [4](#0-3) 

This trust assumption is the root cause: the governance canister conflates *"caller is a valid Swap canister"* with *"caller is the Swap canister for this proposal"*.

The analogous Seaport/ClearingHouse pattern is exact: just as anyone could craft a Seaport order whose consideration recipient was a ClearingHouse with a genuine `collateralId`, any deployed SNS Swap canister can craft a `SettleNeuronsFundParticipationRequest` whose `nns_proposal_id` belongs to a different SNS.

### Impact Explanation

If a malicious SNS Swap canister exploits this:

- **Lifecycle corruption**: The targeted SNS proposal's `sns_token_swap_lifecycle` is irreversibly set to `Committed` or `Aborted` before the genuine swap has concluded. Once set, the lock prevents the legitimate Swap from settling, causing it to revert permanently.
- **ICP minting to wrong address**: In the `Committed` path, ICP is minted from Neurons' Fund maturity and sent to the attacker-supplied `sns_governance_canister_id`, draining Neurons' Fund maturity to an attacker-controlled canister.
- **Incorrect maturity refunds**: In the `Aborted` path, maturity reserved for the targeted SNS is refunded prematurely, before the genuine swap outcome is known.
- **Denial of finalization**: The genuine Swap's subsequent call to `settle_neurons_fund_participation` will fail because the lifecycle field is already set, permanently blocking finalization.

### Likelihood Explanation

Exploitation requires the attacker to control a canister that `is_canister_id_valid_swap_canister_id` recognizes as a valid SNS Swap canister. In practice this means:

1. Submitting and passing an NNS governance proposal to create an SNS (the standard SNS creation flow).
2. Upgrading the resulting SNS Swap canister — via a second NNS governance proposal — to include code that calls `settle_neurons_fund_participation` with a victim SNS's `nns_proposal_id`.

Both steps require NNS governance approval, which is a significant barrier. However, the vulnerability is structural: the authorization check is provably insufficient, the code comment acknowledges it, and no on-chain mechanism prevents a future SNS Swap canister (whether through a governance-approved upgrade or a future code path) from exploiting it. The impact when triggered is severe and irreversible.

### Recommendation

After the `is_canister_id_valid_swap_canister_id` check passes, retrieve the `target_swap_canister_id` stored in the `ProposalData` for `nns_proposal_id` (set at SNS creation time by SNS-W) and assert it equals `caller`. This binds each settlement call to the one Swap canister that owns the proposal, eliminating cross-settlement.

```rust
// After is_canister_id_valid_swap_canister_id passes:
let expected_swap_canister_id = proposal_data
    .swap_canister_id()  // stored at proposal creation
    .ok_or_else(|| GovernanceError::new_with_message(
        ErrorType::PreconditionFailed,
        "Proposal has no associated swap canister id",
    ))?;
if target_canister_id != expected_swap_canister_id {
    return Err(GovernanceError::new_with_message(
        ErrorType::NotAuthorized,
        format!("Caller {caller} is not the swap canister for proposal {}", request.nns_proposal_id),
    ));
}
```

### Proof of Concept

1. Attacker submits an NNS proposal to create SNS-A; it is approved and SNS-A's Swap canister (`swap_A`) is deployed by SNS-W.
2. Attacker submits an NNS upgrade proposal for `swap_A` that adds a method `attack(nns_proposal_id, victim_sns_gov_id)` which calls `nns_governance.settle_neurons_fund_participation(SettleNeuronsFundParticipationRequest { nns_proposal_id, result: Committed { sns_governance_canister_id: victim_sns_gov_id, total_direct_participation_icp_e8s: MAX, ... } })`.
3. Once the upgrade is approved, attacker calls `swap_A.attack(victim_proposal_id, attacker_canister_id)`.
4. NNS Governance passes the `is_canister_id_valid_swap_canister_id` check (swap_A is a real SNS Swap), fetches the victim proposal's `NeuronsFundParticipation`, mints ICP to `attacker_canister_id`, and sets the victim proposal lifecycle to `Committed`.
5. The genuine victim Swap's `finalize_swap` → `settle_neurons_fund_participation` call now fails because the lifecycle is already set, permanently blocking the victim SNS from finalizing. [5](#0-4) [2](#0-1) [6](#0-5)

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

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2508-2519)
```text
// This design assumes trust between the Neurons' Fund and the SNS Swap canisters. In the one hand,
// the Swap trusts that the Neurons' Fund sends the correct amount of ICP to the SNS treasury,
// and that the Neurons' Fund allocates its participants following the Matched Funding rules. On the
// other hand, the Neurons' Fund trusts that the Swap will indeed create appropriate SNS neurons
// for the Neurons' Fund participants.
//
// The justification for this trust assumption is as follows. The Neurons' Fund can be trusted as
// it is controlled by the NNS. The SNS Swap can be trusted as it is (1) deployed by SNS-W, which is
// also part of the NNS and (2) upgraded via an NNS proposal (unlike all other SNS canisters).
//
// This request may be submitted only by the Swap canister of an SNS instance created by
// a CreateServiceNervousSystem proposal.
```
