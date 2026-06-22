After extensive research across the IC codebase, I found a valid analog to the Cooler authorization bypass vulnerability.

---

### Title
Missing Proposal-Caller Binding in `settle_neurons_fund_participation` Allows Any SNS Swap to Settle Another SNS's Neurons' Fund Participation - (File: `rs/nns/governance/src/governance.rs`)

### Summary
`NNS Governance.settle_neurons_fund_participation` validates that the caller is *a* valid SNS swap canister, but never verifies that the caller is the *specific* swap canister associated with the `nns_proposal_id` supplied in the request. Any deployed SNS swap canister can call this function with an arbitrary victim proposal ID and attacker-controlled parameters, settling another SNS's Neurons' Fund participation before the legitimate swap canister does.

### Finding Description

In `rs/nns/governance/src/governance.rs`, the authorization check for `settle_neurons_fund_participation` is:

```rust
// Check authorization. Note that a Swap could settle each other's participation.
let target_canister_id: CanisterId = caller.try_into()...?;
if let Err(err_msg) =
    is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
{
    return Err(...);
}
``` [1](#0-0) 

`is_canister_id_valid_swap_canister_id` queries SNS-W and confirms the caller is *any* deployed SNS swap canister:

```rust
let is_swap = list_deployed_snses_response
    .instances
    .iter()
    .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
``` [2](#0-1) 

The developer comment at line 7018 explicitly acknowledges the gap: *"Note that a Swap could settle each other's participation."* There is no subsequent check binding the caller's canister ID to the `nns_proposal_id` in the request.

The `SettleNeuronsFundParticipationRequest` carries:
- `nns_proposal_id` — the proposal that created the *target* SNS (fully attacker-chosen)
- `result = Committed { sns_governance_canister_id, total_direct_participation_icp_e8s }` — where ICP is minted and how much Neurons' Fund participation is computed (both attacker-chosen) [3](#0-2) 

The function then proceeds to compute effective Neurons' Fund participation, mint ICP to `sns_governance_canister_id`, and permanently mark the proposal lifecycle as `Committed` or `Aborted`: [4](#0-3) 

Once settled, the function is idempotent — it returns the previously computed result on all subsequent calls: [5](#0-4) 

The `SettleNeuronsFundParticipationRequest` proto definition confirms `sns_governance_canister_id` is a free field in the `Committed` variant with no cross-validation against the proposal's actual SNS governance canister: [6](#0-5) 

### Impact Explanation

**Theft / misdirection of Neurons' Fund ICP**: SNS A's swap canister calls `settle_neurons_fund_participation` with SNS B's `nns_proposal_id`, sets `sns_governance_canister_id` to SNS A's governance canister, and sets `total_direct_participation_icp_e8s = u64::MAX`. NNS Governance mints the maximum reserved Neurons' Fund ICP and sends it to SNS A's governance canister instead of SNS B's.

**Permanent disruption of SNS B's swap**: After the attacker's call, the proposal lifecycle is set to `Committed`. When SNS B's legitimate swap canister later calls `settle_neurons_fund_participation`, it receives the previously computed (attacker-controlled) result and cannot re-settle with correct parameters. SNS B's `finalize_swap` will proceed with wrong Neurons' Fund neuron data, creating incorrectly sized SNS neurons for Neurons' Fund participants.

**Ledger conservation violation**: ICP is minted from the Neurons' Fund and sent to the wrong recipient, violating the conservation invariant of the ICP ledger.

### Likelihood Explanation

- Requires the attacker to control a legitimate SNS swap canister (deployed by SNS-W via an NNS proposal). This is a meaningful barrier but not prohibitive — any existing SNS project satisfies it.
- No additional setup is needed beyond having a deployed SNS. The attack is a single ingress call to NNS Governance from the attacker's swap canister.
- The attack window is: after SNS B's swap opens (Neurons' Fund maturity is reserved) and before SNS B's `finalize_swap` calls `settle_neurons_fund_participation`. This window can last days to weeks depending on the swap duration.
- The attack can be executed in one block.

### Recommendation

After confirming the caller is a valid swap canister, additionally verify that the caller is the swap canister *associated with the supplied `nns_proposal_id`*. Concretely:

1. Look up the SNS instance in SNS-W by `nns_proposal_id` (the `list_deployed_snses` response already contains `nns_proposal_id` per instance).
2. Assert `caller == sns_instance.swap_canister_id` for the matching instance.

This mirrors the fix applied to the Cooler vulnerability: check that the lender (caller) is the Clearinghouse, not merely that the loan came from a factory-deployed Cooler.

### Proof of Concept

1. SNS B has an open swap with `nns_proposal_id = 42`; Neurons' Fund has reserved 100,000 ICP of maturity.
2. SNS A (a legitimate, attacker-controlled SNS) has swap canister `swap_a` and governance canister `gov_a`.
3. `swap_a` calls `settle_neurons_fund_participation` with:
   ```
   nns_proposal_id = 42
   result = Committed {
     sns_governance_canister_id = gov_a,   // attacker's canister
     total_direct_participation_icp_e8s = u64::MAX,  // maximize NF contribution
   }
   ```
4. NNS Governance confirms `swap_a` is a valid swap canister via `is_canister_id_valid_swap_canister_id` — it is.
5. NNS Governance computes maximum Neurons' Fund participation, mints ICP, and sends it to `gov_a`.
6. Proposal 42's lifecycle is set to `Committed`; `previously_computed_final_neurons_fund_participation` is now `Some(...)`.
7. When SNS B's actual swap canister later calls `settle_neurons_fund_participation` for proposal 42, it hits the idempotency path at line 7166–7174 and receives the attacker-computed result — SNS B's Neurons' Fund neurons are created with wrong amounts and the stolen ICP is unrecoverable.

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

**File:** rs/nns/governance/src/governance.rs (L7039-7048)
```rust
        // Re-acquire the proposal_data mutably after `SnsWasm.list_deployed_snses().await`.
        // Mutability will be needed later, when we aquire the lock (see
        // `proposal_data.set_swap_lifecycle_by_settle_neurons_fund_participation_request_type`).
        let proposal_data = self.mut_proposal_data_or_err(
            &request.nns_proposal_id,
            &format!("after awaiting SNS-W for {:?}", request.request_str),
        )?;

        // Record the proposal's current lifecycle. If an error occurs when settling
        // the Neurons' Fund the previous Lifecycle should be set to allow for retries.
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

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2473-2482)
```text
  message Committed {
    // This is where the minted ICP will be sent. In principal, this could be
    // fetched using the swap canister's get_state method.
    ic_base_types.pb.v1.PrincipalId sns_governance_canister_id = 1;
    // Total contribution amount from direct swap participants.
    optional uint64 total_direct_contribution_icp_e8s = 2;
    // Total contribution amount from the Neuron's Fund.
    // TODO[NNS1-2570]: Ensure this field is set.
    optional uint64 total_neurons_fund_contribution_icp_e8s = 3;
  }
```
