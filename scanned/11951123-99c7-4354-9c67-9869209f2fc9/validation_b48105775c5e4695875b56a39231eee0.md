### Title
Missing Per-Proposal Swap Canister Initiator Validation in `settle_neurons_fund_participation` - (File: rs/nns/governance/src/governance.rs)

### Summary

`settle_neurons_fund_participation` in NNS Governance validates only that the caller is *some* valid SNS Swap canister, not that it is the *specific* Swap canister associated with the `nns_proposal_id` supplied in the request. The code itself acknowledges this gap with the comment "Note that a Swap could settle each other's participation." Any deployed SNS Swap canister can therefore trigger Neurons' Fund settlement — including ICP minting — for a completely different SNS's proposal, supplying attacker-controlled fields such as `sns_governance_canister_id`.

### Finding Description

The two-step SNS lifecycle is:

1. NNS Governance executes a `CreateServiceNervousSystem` proposal, which causes SNS-W to deploy a Swap canister and reserves Neurons' Fund maturity for that specific SNS instance.
2. When the swap ends, the Swap canister calls `settle_neurons_fund_participation` on NNS Governance, which mints ICP to the SNS treasury and refunds leftover maturity.

The handler in `governance.rs` performs two authorization checks:

```rust
// Check authorization. Note that a Swap could settle each other's participation.
let target_canister_id: CanisterId = caller.try_into()...?;
if let Err(err_msg) =
    is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
{
    return Err(...NotAuthorized...);
}
```

`is_canister_id_valid_swap_canister_id` queries SNS-W and confirms only that the caller appears in the global list of deployed SNS Swap canisters:

```rust
let is_swap = list_deployed_snses_response
    .instances
    .iter()
    .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
```

There is no check that `caller` is the Swap canister whose `init.nns_proposal_id` matches `request.nns_proposal_id`. The `Committed` variant of the request also carries a caller-supplied `sns_governance_canister_id` field that is used directly as the ICP mint destination:

```rust
let destination =
    AccountIdentifier::new(sns_governance_canister_id, /* subaccount = */ None);
let _ = self.ledger.transfer_funds(amount_icp_e8s, 0, None, destination, 0).await...
```

No cross-check against the proposal's own SNS governance canister ID is performed before minting.

### Impact Explanation

A malicious SNS Swap canister (Swap-A, legitimately deployed for SNS-A) can call `settle_neurons_fund_participation` with:

- `nns_proposal_id` = the proposal ID of a *different* SNS-B that has Neurons' Fund participation reserved but not yet settled
- `result = Committed` with `sns_governance_canister_id` = an attacker-controlled principal
- `total_direct_participation_icp_e8s` = a value chosen to maximize Neurons' Fund ICP minting

NNS Governance will:
1. Mint Neurons' Fund ICP and transfer it to the attacker-controlled address instead of SNS-B's treasury.
2. Mark SNS-B's proposal lifecycle as `Committed`, permanently preventing SNS-B's legitimate Swap canister from settling correctly.
3. Consume Neurons' Fund maturity that should have been refunded to NNS neurons.

The financial impact is bounded by the total Neurons' Fund maturity reserved for SNS-B's swap, which can be in the millions of ICP range for large SNS launches.

### Likelihood Explanation

The attacker must control a legitimately deployed SNS Swap canister, which requires an approved NNS `CreateServiceNervousSystem` proposal. This is a non-trivial but realistic barrier: the IC ecosystem already has dozens of deployed SNS instances, each with a Swap canister that satisfies the `is_canister_id_valid_swap_canister_id` check. Any operator of an existing SNS Swap canister — or anyone who can get a new SNS approved — can execute this attack against any other SNS whose Neurons' Fund settlement has not yet been finalized. The attack window is the period between SNS-B's swap reaching a terminal lifecycle state and SNS-B's own Swap canister calling `finalize`.

### Recommendation

After confirming the caller is a valid SNS Swap canister, additionally verify that the caller's canister ID matches the Swap canister recorded for the specific `nns_proposal_id` in the request. The proposal data already contains the SNS deployment information (accessible via SNS-W's `list_deployed_snses` response, which is already fetched during `is_canister_id_valid_swap_canister_id`). The fix is to cross-reference the caller against the `swap_canister_id` of the SNS instance whose `nns_proposal_id` matches the request, rather than accepting any swap canister from the global list.

Additionally, `sns_governance_canister_id` supplied in the `Committed` payload should be validated against the SNS governance canister ID recorded in the proposal data, rather than used as-is as the ICP mint destination.

### Proof of Concept

1. Attacker controls SNS-A's Swap canister (legitimately deployed, passes `is_canister_id_valid_swap_canister_id`).
2. SNS-B has a live `CreateServiceNervousSystem` proposal (ID = `P_B`) with Neurons' Fund participation reserved; its swap is in `Committed` state but `finalize` has not been called yet.
3. Attacker's Swap canister sends an ingress call to NNS Governance `settle_neurons_fund_participation` with:
   ```
   nns_proposal_id = P_B
   result = Committed {
     sns_governance_canister_id = <attacker_principal>,
     total_direct_participation_icp_e8s = <max_value>,
     total_neurons_fund_participation_icp_e8s = <max_value>,
   }
   ```
4. `is_canister_id_valid_swap_canister_id` passes because Swap-A is a valid swap canister.
5. No check verifies Swap-A is the swap canister for proposal `P_B`.
6. NNS Governance computes Neurons' Fund participation for `P_B`, mints ICP, and transfers it to `<attacker_principal>`.
7. SNS-B's proposal lifecycle is set to `Committed`; SNS-B's own Swap canister's subsequent `finalize` call returns the cached (attacker-supplied) result.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2)

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
