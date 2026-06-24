Audit Report

## Title
Missing Proposal-Caller Binding in `settle_neurons_fund_participation` Allows Any SNS Swap to Settle Another SNS's Neurons' Fund Participation - (File: `rs/nns/governance/src/governance.rs`)

## Summary
`NNS Governance.settle_neurons_fund_participation` verifies the caller is *a* valid SNS swap canister via SNS-W, but never checks that the caller is the swap canister *associated with the supplied `nns_proposal_id`*. Any deployed SNS swap canister can call this function with an arbitrary victim proposal ID and an attacker-controlled `sns_governance_canister_id`, causing Neurons' Fund ICP to be minted and sent to the attacker's governance canister while permanently locking the victim SNS's settlement state.

## Finding Description
The authorization check at lines 7018–7038 of `rs/nns/governance/src/governance.rs` converts the caller to a `CanisterId` and calls `is_canister_id_valid_swap_canister_id`, which queries SNS-W's `list_deployed_snses` and performs only an `any()` membership check:

```rust
let is_swap = list_deployed_snses_response
    .instances
    .iter()
    .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
```

The developer comment at line 7018 explicitly acknowledges the gap: *"Note that a Swap could settle each other's participation."* No subsequent check binds the caller to the specific SNS instance associated with `request.nns_proposal_id`. The `list_deployed_snses_response.instances` already carries `nns_proposal_id` per instance, but this is never cross-referenced.

After passing authorization, the function extracts `sns_governance_canister_id` directly from the attacker-controlled `Committed` payload (line 7249–7254) and passes it to `mint_to_sns_governance` (line 7266–7273):

```rust
let destination = AccountIdentifier::new(sns_governance_canister_id, /* subaccount = */ None);
```

The ICP ledger mints to this attacker-supplied address. No validation against the proposal's actual SNS governance canister is performed anywhere in `ValidatedSettleNeuronsFundParticipationRequest::try_from` or in the function body.

Once the call succeeds, `final_neurons_fund_participation` is written to the proposal's `neurons_fund_data` and the lifecycle is set to `Committed`. The idempotency branch at lines 7166–7174 then returns the cached (attacker-computed) result to all subsequent callers, including the legitimate swap canister.

## Impact Explanation
This matches the **High** impact class: *"Unauthorized access to neurons, governance assets, wallets, identities, ledgers, or canister-controlled funds where exploitation requires meaningful per-target work or other constraints."* Concretely:

- Neurons' Fund ICP (maturity converted to ICP and minted) is sent to the attacker's governance canister instead of the victim SNS's governance canister. The amount is bounded by the victim SNS's `max_direct_participation_icp_e8s` cap and the Neurons' Fund size, but can be substantial (potentially millions of ICP e8s for a large SNS swap).
- The victim SNS B's proposal lifecycle is permanently set to `Committed` with attacker-controlled neuron data. SNS B's legitimate `finalize_swap` call hits the idempotency path and receives wrong Neurons' Fund neuron portions, creating incorrectly sized SNS neurons for all Neurons' Fund participants. This is irreversible without an NNS hotfix.
- If the Neurons' Fund participation for the victim SNS exceeds ~$1M in value, this escalates to the **Critical** tier ("Theft, permanent loss, illegal minting… especially over $1M").

## Likelihood Explanation
- **Precondition**: The attacker must control a legitimate SNS swap canister, which requires a successful NNS `CreateServiceNervousSystem` proposal. This is a meaningful but not prohibitive barrier — any existing SNS project satisfies it.
- **Attack window**: Opens when SNS B's swap opens (Neurons' Fund maturity is reserved) and closes when SNS B's `finalize_swap` calls `settle_neurons_fund_participation`. This window spans the entire swap duration (days to weeks).
- **Execution**: A single inter-canister call from the attacker's swap canister to NNS Governance. No additional setup, no victim interaction, no race condition beyond the window above.
- **Repeatability**: One attacker-controlled SNS can attack any number of victim SNS swaps that are open concurrently.

## Recommendation
After confirming the caller is a valid swap canister, additionally verify that the caller is the swap canister *for the specific `nns_proposal_id`* in the request. The `list_deployed_snses_response` already contains `nns_proposal_id` per instance; the fix is to replace the `any()` membership check with a lookup by `nns_proposal_id` and assert `caller == matched_instance.swap_canister_id`.

Additionally, `sns_governance_canister_id` from the `Committed` payload should be validated against the SNS governance canister recorded in the proposal data (or looked up from SNS-W by `nns_proposal_id`) rather than trusted as supplied by the caller.

## Proof of Concept
1. SNS B has an open swap with `nns_proposal_id = 42`; Neurons' Fund has reserved maturity for this swap.
2. Attacker controls SNS A with swap canister `swap_a` and governance canister `gov_a`.
3. `swap_a` calls `NNS Governance.settle_neurons_fund_participation` with:
   ```
   nns_proposal_id = 42
   result = Committed {
     sns_governance_canister_id = gov_a,
     total_direct_participation_icp_e8s = <max allowed by SNS B's swap params>,
     total_neurons_fund_participation_icp_e8s = <any value>,
   }
   ```
4. NNS Governance confirms `swap_a` is a valid swap canister — it is. No proposal-caller binding check exists.
5. NNS Governance computes maximum Neurons' Fund participation for proposal 42, mints ICP, and sends it to `gov_a`.
6. Proposal 42's lifecycle is set to `Committed`; `final_neurons_fund_participation` is now stored with attacker-controlled data.
7. When SNS B's actual swap canister calls `settle_neurons_fund_participation` for proposal 42, it hits the idempotency path at lines 7166–7174 and receives the attacker-computed result. SNS B's Neurons' Fund neurons are created with wrong amounts; the stolen ICP is unrecoverable without an NNS hotfix.

A deterministic integration test can reproduce this by: (a) deploying two SNS instances via `PocketIC`, (b) calling `settle_neurons_fund_participation` from SNS A's swap canister with SNS B's proposal ID and SNS A's governance canister ID, and (c) asserting that the ICP ledger transfer destination is `gov_a` and that SNS B's subsequent settlement call returns the attacker-computed snapshot. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rs/nns/governance/src/governance.rs (L7464-7496)
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
