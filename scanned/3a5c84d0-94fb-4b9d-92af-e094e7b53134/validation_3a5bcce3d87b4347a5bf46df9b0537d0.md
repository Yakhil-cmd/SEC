### Title
Any Valid SNS Swap Canister Can Settle Another SNS's Neurons' Fund Participation with Attacker-Controlled `sns_governance_canister_id`, Redirecting Minted ICP - (File: rs/nns/governance/src/governance.rs)

### Summary

`settle_neurons_fund_participation` in NNS Governance authorizes the caller by checking only that it is **any** valid SNS swap canister registered in SNS-W, not the **specific** swap canister associated with the proposal being settled. Additionally, the `sns_governance_canister_id` field in the `Committed` request — which determines where minted ICP is sent — is taken directly from caller-supplied input without validation against the proposal's stored SNS governance canister ID. A malicious SNS swap canister can therefore call this endpoint for a victim SNS's proposal, supply its own governance canister ID as the mint destination, and steal the Neurons' Fund ICP while permanently corrupting the victim SNS's settlement state.

### Finding Description

**Root cause — authorization check validates class, not identity:**

In `rs/nns/governance/src/governance.rs`, `settle_neurons_fund_participation` performs the following authorization:

```rust
// Check authorization. Note that a Swap could settle each other's participation.
let target_canister_id: CanisterId = caller.try_into()...?;
if let Err(err_msg) =
    is_canister_id_valid_swap_canister_id(target_canister_id, &*self.env).await
{
    return Err(...);
}
``` [1](#0-0) 

`is_canister_id_valid_swap_canister_id` queries SNS-W's `list_deployed_snses` and checks whether the caller appears as **any** swap canister across all deployed SNS instances:

```rust
let is_swap = list_deployed_snses_response
    .instances
    .iter()
    .any(|sns| sns.swap_canister_id == Some(target_canister_id.into()));
``` [2](#0-1) 

There is no check that the caller is the swap canister **associated with the specific `nns_proposal_id`** in the request. The code comment explicitly acknowledges this: *"Note that a Swap could settle each other's participation."*

**Root cause — caller-supplied `sns_governance_canister_id` used as mint destination without validation:**

When the result is `Committed`, the `sns_governance_canister_id` field is taken from the caller's request and passed directly to `mint_to_sns_governance`:

```rust
} else if let SwapResult::Committed {
    sns_governance_canister_id,
    ...
} = request.swap_result
{
    let mint_icp_result = self
        .mint_to_sns_governance(
            &request.nns_proposal_id,
            sns_governance_canister_id,   // ← caller-supplied, unvalidated
            ...
        )
        .await;
``` [3](#0-2) 

Inside `mint_to_sns_governance`, this value is used as the ICP transfer destination:

```rust
let destination =
    AccountIdentifier::new(sns_governance_canister_id, /* subaccount = */ None);
let _ = self.ledger.transfer_funds(amount_icp_e8s, 0, None, destination, 0).await...;
``` [4](#0-3) 

The `sns_governance_canister_id` stored in the proposal's `CreateServiceNervousSystem` action is never compared against the caller-supplied value.

**The `settle_neurons_fund_participation` endpoint is a public `#[update]` method:**

```rust
#[update]
async fn settle_neurons_fund_participation(
    request: SettleNeuronsFundParticipationRequest,
) -> SettleNeuronsFundParticipationResponse {
    ...
    governance_mut()
        .settle_neurons_fund_participation(caller(), request.into())
        .await;
``` [5](#0-4) 

**Exploit path:**

1. Attacker creates a legitimate SNS (SNS A) through the normal NNS governance proposal process. This gives the attacker a valid `swap_canister_id` registered in SNS-W.
2. A victim SNS (SNS B) has an active swap with Neurons' Fund participation enabled (`neurons_fund_participation = true`), and its proposal has `initial_neurons_fund_participation` set.
3. Attacker's Swap A calls `settle_neurons_fund_participation` on NNS Governance with:
   - `nns_proposal_id` = SNS B's proposal ID
   - `result = Committed { sns_governance_canister_id: SNS_A_governance, total_direct_participation_icp_e8s: u64::MAX, ... }`
4. Authorization passes: Swap A is a valid swap canister in SNS-W's registry.
5. Proposal check passes: SNS B's proposal is a `CreateServiceNervousSystem`.
6. State machine check passes: first call, lifecycle not yet terminal.
7. NNS Governance computes Neurons' Fund participation using the attacker-supplied `total_direct_participation_icp_e8s` (maximizing the minted amount).
8. NNS Governance mints ICP and sends it to SNS A's governance canister (attacker-controlled).
9. SNS B's proposal lifecycle is set to `Committed` (terminal), permanently preventing legitimate settlement.

The `SettleNeuronsFundParticipationRequest.Committed` struct that the attacker controls:

```rust
pub struct Committed {
    pub sns_governance_canister_id: Option<PrincipalId>,  // attacker sets this to SNS A
    pub total_direct_participation_icp_e8s: Option<u64>,  // attacker maximizes this
    pub total_neurons_fund_participation_icp_e8s: Option<u64>,
}
``` [6](#0-5) 

### Impact Explanation

**Governance authorization bug / ledger conservation bug.**

- **ICP theft**: Neurons' Fund ICP (potentially millions of ICP, depending on the SNS's Matched Funding parameters) is minted and sent to the attacker's governance canister instead of the victim SNS's treasury.
- **Permanent state corruption**: SNS B's proposal lifecycle is set to `Committed` (terminal). The idempotency check at line 7166 will return the previously computed (attacker-manipulated) result on any subsequent call, making the corruption irreversible without an NNS upgrade.
- **Neurons' Fund neuron harm**: NNS neurons that contributed maturity to SNS B's Neurons' Fund participation lose their maturity permanently, receiving nothing in return (no SNS neurons are created for them in SNS B).

### Likelihood Explanation

**Low-Medium.** The precondition is that the attacker must control a legitimate SNS swap canister, which requires passing an NNS governance vote to create an SNS. This is a meaningful barrier but not impossible — the NNS governance process is open to any sufficiently staked neuron holder. Once an SNS is created, the attack is a single canister call with no further preconditions. The attack window is the period between SNS B's swap committing and its legitimate `settle_neurons_fund_participation` call being processed.

### Recommendation

1. **Bind the caller to the proposal**: After fetching the proposal data for `nns_proposal_id`, extract the swap canister ID from the `CreateServiceNervousSystem` action's deployed SNS record and assert `caller == proposal_swap_canister_id`. This eliminates the cross-swap settlement attack entirely.

2. **Validate `sns_governance_canister_id` against proposal data**: When processing a `Committed` result, retrieve the `sns_governance_canister_id` from the proposal's stored SNS deployment record (not from the caller's request) and use that as the mint destination. The caller-supplied value should be treated as advisory only (or ignored entirely).

### Proof of Concept

```
// Attacker controls SNS A swap canister (canister ID: swap_a)
// Victim is SNS B with proposal_id = 42, governance = gov_b

// Call from swap_a to NNS Governance:
settle_neurons_fund_participation(SettleNeuronsFundParticipationRequest {
    nns_proposal_id: Some(42),  // SNS B's proposal
    result: Some(Result::Committed(Committed {
        sns_governance_canister_id: Some(gov_a),  // SNS A's governance (attacker-controlled)
        total_direct_participation_icp_e8s: Some(u64::MAX),  // maximize minted ICP
        total_neurons_fund_participation_icp_e8s: Some(u64::MAX),
    })),
})

// Authorization check: is swap_a in list_deployed_snses? YES → passes
// Proposal check: is proposal 42 a CreateServiceNervousSystem? YES → passes
// State machine: first call, not terminal → proceeds
// Result: NNS mints Neurons' Fund ICP to gov_a (attacker), proposal 42 marked terminal
// SNS B's legitimate settlement is now permanently blocked
```

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
