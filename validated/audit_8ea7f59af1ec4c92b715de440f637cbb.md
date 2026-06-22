Based on my code investigation, here is the analysis:

## Key Code Findings

**Authorization check in `settle_neurons_fund_participation`** (`rs/nns/governance/src/governance.rs`):

The function checks that the caller is a valid SNS swap canister via `is_canister_id_valid_swap_canister_id`, but the code comment at line 7018 explicitly states:

> "Check authorization. Note that a Swap could settle each other's participation."

This means the check only verifies the caller is **any** valid SNS swap canister registered with SNS-W — not the specific swap canister associated with the target proposal. [1](#0-0) 

**`mint_to_sns_governance` uses the caller-supplied ID directly**:

```rust
let destination =
    AccountIdentifier::new(sns_governance_canister_id, /* subaccount = */ None);
```

There is no cross-check of `sns_governance_canister_id` against the proposal data's recorded SNS governance canister. [2](#0-1) 

**The `sns_governance_canister_id` is caller-supplied in the `Committed` variant**: [3](#0-2) 

**The call site passes the request field directly to `mint_to_sns_governance`**: [4](#0-3) 

---

### Title
Caller-Supplied `sns_governance_canister_id` Used as ICP Mint Destination Without Proposal Cross-Check — (`rs/nns/governance/src/governance.rs`)

### Summary
Any legitimately deployed SNS swap canister can call `settle_neurons_fund_participation` for **any** NNS proposal (not just its own), supplying an arbitrary `sns_governance_canister_id` in the `Committed` variant. Because `mint_to_sns_governance` uses this caller-supplied principal as the ICP ledger mint destination without validating it against the SNS governance canister recorded in the proposal data, an attacker controlling a legitimate SNS swap canister can redirect Neurons' Fund ICP minting to an attacker-controlled account.

### Finding Description
The authorization guard at line 7028 calls `is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await`, which queries SNS-W to verify the caller is **any** registered SNS swap canister. The code comment explicitly acknowledges: *"Note that a Swap could settle each other's participation."* This means:

1. Attacker deploys a legitimate SNS (via a `CreateServiceNervousSystem` proposal), obtaining a valid SNS swap canister ID recognized by SNS-W.
2. Attacker's swap canister calls `settle_neurons_fund_participation` targeting a **victim** proposal's `nns_proposal_id`, with `Committed { sns_governance_canister_id = attacker_principal, total_direct_participation_icp_e8s = max_direct }`.
3. The governance accepts the call (caller is a valid swap canister), looks up the victim proposal's `initial_neurons_fund_participation`, computes the NF contribution, and calls `mint_to_sns_governance(attacker_principal, amount_icp_e8s)`.
4. The ICP ledger mints funds to `AccountIdentifier::new(attacker_principal, None)` — an attacker-controlled account.

The proposal data is never consulted for the correct SNS governance canister ID. [5](#0-4) 

### Impact Explanation
The Neurons' Fund can contribute up to 10% of total NF maturity to a single SNS swap. At current ICP valuations, this can exceed $1M. The minted ICP is sent to the attacker's account instead of the legitimate SNS treasury, constituting direct theft of minted ICP.

### Likelihood Explanation
The precondition — controlling a legitimately deployed SNS swap canister — requires passing an NNS governance vote, which is non-trivial but achievable. The attack must be timed before the legitimate swap canister settles (front-running the legitimate `settle_neurons_fund_participation` call). The lifecycle idempotency check at line 7166 means the attack only works on the **first** settlement call for a given proposal. [6](#0-5) 

### Recommendation
In `settle_neurons_fund_participation`, after looking up the proposal data, cross-check the caller against the proposal's recorded swap canister ID (not just any SNS-W-registered swap), and cross-check the `sns_governance_canister_id` from the request against the SNS governance canister recorded in the proposal's `CreateServiceNervousSystem` deployment data. Reject the call if either does not match.

### Proof of Concept
```rust
// Attacker's swap canister sends:
settle_neurons_fund_participation(SettleNeuronsFundParticipationRequest {
    nns_proposal_id: Some(victim_proposal_id),
    result: Some(Result::Committed(Committed {
        sns_governance_canister_id: Some(attacker_principal), // arbitrary
        total_direct_participation_icp_e8s: Some(max_direct_participation),
        total_neurons_fund_participation_icp_e8s: Some(0),
    })),
})
// Governance accepts (caller is a valid swap canister per SNS-W),
// computes NF amount from victim proposal's initial_neurons_fund_participation,
// calls mint_to_sns_governance(attacker_principal, amount_icp_e8s),
// ICP ledger mints to AccountIdentifier::new(attacker_principal, None).
``` [7](#0-6)

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

**File:** rs/nns/governance/src/governance.rs (L7464-7506)
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
```

**File:** rs/nns/governance/api/src/types.rs (L7547-7555)
```rust

```
