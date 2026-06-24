### Title
Any Valid SNS Swap Canister Can Settle Another SNS's Neurons' Fund Participation and Redirect Minted ICP - (`rs/nns/governance/src/governance.rs`)

### Summary

`Governance::settle_neurons_fund_participation` in NNS Governance verifies only that the caller is *any* valid SNS swap canister, but never checks that the caller is the *specific* swap canister associated with the `nns_proposal_id` supplied in the request. This is the direct IC analog of the `buyLoan()` bug: an attacker-controlled swap canister can supply a victim SNS's proposal ID, force settlement of the victim's Neurons' Fund participation, and redirect the minted ICP to its own governance canister.

### Finding Description

The authorization logic in `settle_neurons_fund_participation` is:

```rust
// Check authorization. Note that a Swap could settle each other's participation.
let target_canister_id: CanisterId = caller.try_into()...?;
if let Err(err_msg) =
    is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
{
    return Err(...NotAuthorized...);
}
``` [1](#0-0) 

`is_canister_id_valid_swap_canister_id` only checks whether the caller appears in the global list of deployed SNS swap canisters returned by SNS-W:

```rust
let is_swap = list_deployed_snses_response
    .instances
    .iter()
    .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
``` [2](#0-1) 

There is no check that `caller == swap_canister_id_for(request.nns_proposal_id)`. The `nns_proposal_id` in the request is used to look up the victim proposal's `NeuronsFundData`, and the `sns_governance_canister_id` field inside the `Committed` variant is taken directly from the request and used as the ICP mint destination:

```rust
let destination =
    AccountIdentifier::new(sns_governance_canister_id, /* subaccount = */ None);
let _ = self.ledger.transfer_funds(amount_icp_e8s, 0, None, destination, 0).await...
``` [3](#0-2) 

The `sns_governance_canister_id` is never validated against the governance canister recorded for the proposal being settled.

### Impact Explanation

A malicious SNS Swap canister (SNS-A) that is legitimately deployed by SNS-W can:

1. Call `settle_neurons_fund_participation` on NNS Governance with:
   - `nns_proposal_id` = victim SNS-B's proposal ID
   - `result = Committed { sns_governance_canister_id: SNS-A's own governance canister, total_direct_participation_icp_e8s: <max value> }`
2. NNS Governance passes the authorization check (SNS-A is a valid swap canister).
3. NNS Governance computes the Neurons' Fund ICP amount from SNS-B's proposal data and **mints it to SNS-A's governance canister** instead of SNS-B's.
4. SNS-B's proposal lifecycle is set to `Committed` (terminal), making the settlement idempotent and preventing SNS-B from ever legitimately settling its own Neurons' Fund participation.

Alternatively, SNS-A can call with `result = Aborted` to force SNS-B's Neurons' Fund maturity to be refunded to NNS neurons, permanently denying SNS-B its Matched Funding ICP even if SNS-B's swap actually committed.

The comment in the code itself acknowledges this gap: *"Note that a Swap could settle each other's participation."* [4](#0-3) 

### Likelihood Explanation

The attacker must control a legitimately deployed SNS swap canister. SNS deployments require an NNS governance proposal (`CreateServiceNervousSystem`), so the attacker must pass NNS governance. However, once an SNS is deployed, its swap canister is under the control of the SNS's own governance, which can be controlled by the SNS's token holders. Any SNS whose token distribution is sufficiently concentrated can exploit this against any other SNS that has an open or recently-committed swap with Neurons' Fund participation enabled. The number of deployed SNSes on mainnet makes this a realistic cross-SNS attack surface.

### Recommendation

After the `is_canister_id_valid_swap_canister_id` check passes, add a binding check that the caller is the swap canister recorded for the specific proposal:

```rust
// After the generic swap-canister check, verify the caller is the
// swap canister for THIS proposal specifically.
let proposal_swap_canister_id = proposal_data
    .get_swap_canister_id_for_proposal()  // retrieve from DeployNewSnsResponse stored on proposal
    .ok_or_else(|| GovernanceError::new_with_message(
        ErrorType::NotAuthorized,
        format!("Proposal {:?} has no associated swap canister.", request.nns_proposal_id),
    ))?;
if caller != proposal_swap_canister_id.get() {
    return Err(GovernanceError::new_with_message(
        ErrorType::NotAuthorized,
        format!(
            "Caller {caller} is not the swap canister for proposal {:?}.",
            request.nns_proposal_id
        ),
    ));
}
```

Additionally, validate that `sns_governance_canister_id` in the `Committed` variant matches the governance canister recorded for the proposal, rather than using it as a free-form mint destination.

### Proof of Concept

1. SNS-A is deployed via NNS governance proposal P-A. SNS-B is deployed via proposal P-B, with Neurons' Fund participation enabled. SNS-B's swap commits.
2. Before SNS-B's swap canister calls `settle_neurons_fund_participation`, SNS-A's governance upgrades SNS-A's swap canister with malicious code.
3. SNS-A's swap canister calls NNS Governance's `settle_neurons_fund_participation` with:
   ```
   nns_proposal_id = P-B.id
   result = Committed {
       sns_governance_canister_id = SNS-A's governance canister principal,
       total_direct_participation_icp_e8s = <SNS-B's actual direct participation>,
       total_neurons_fund_participation_icp_e8s = <SNS-B's NF participation>,
   }
   ```
4. NNS Governance verifies SNS-A is a valid swap canister (passes `is_canister_id_valid_swap_canister_id`).
5. NNS Governance computes Neurons' Fund ICP from P-B's `NeuronsFundData` and mints it to SNS-A's governance canister.
6. P-B's `sns_token_swap_lifecycle` is set to `Committed` (terminal).
7. When SNS-B's legitimate swap canister later calls `settle_neurons_fund_participation`, it hits the idempotency path (Ok case I) and returns the already-recorded (attacker-controlled) result without minting additional ICP. [5](#0-4) [6](#0-5)

### Citations

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
