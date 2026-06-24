Audit Report

## Title
`ManageLedgerParameters` Not Blocked During `PreInitializationSwap` Mode - (File: `rs/sns/governance/src/types.rs`)

## Summary
`functions_disallowed_in_pre_initialization_swap()` in `rs/sns/governance/src/types.rs` enumerates six proposal actions blocked during `PreInitializationSwap` mode, but omits `ManageLedgerParameters` (Id = 13). Because developer neurons hold 100% of voting power before any swap participant receives tokens, a malicious SNS developer can unilaterally pass a `ManageLedgerParameters` proposal mid-swap to inflate the SNS ledger `transfer_fee`, silently reducing the token allocations of every swap participant during finalization.

## Finding Description
The denylist at `rs/sns/governance/src/types.rs` L253–262 is confirmed in the repository and contains exactly six entries — none of which is `ManageLedgerParameters`:

```rust
pub fn functions_disallowed_in_pre_initialization_swap() -> Vec<NervousSystemFunction> {
    vec![
        NervousSystemFunction::manage_nervous_system_parameters(),
        NervousSystemFunction::transfer_sns_treasury_funds(),
        NervousSystemFunction::mint_sns_tokens(),
        NervousSystemFunction::upgrade_sns_controlled_canister(),
        NervousSystemFunction::register_dapp_canisters(),
        NervousSystemFunction::deregister_dapp_canisters(),
    ]
}
```

The gating function `proposal_action_is_allowed_in_pre_initialization_swap_or_err` at L279–298 performs a simple membership check against this list and returns `Ok(())` for any action not present. `ManageLedgerParameters` therefore passes the check unconditionally.

The execution path at `rs/sns/governance/src/governance.rs` L2213–2216 dispatches directly to `perform_manage_ledger_parameters` with no lifecycle-state guard:

```rust
Action::ManageLedgerParameters(manage_ledger_parameters) => {
    self.perform_manage_ledger_parameters(proposal_id, manage_ledger_parameters)
        .await
}
```

`PreInitializationSwap` mode persists from SNS creation until the swap canister calls `set_mode(Normal)` after successful finalization (confirmed at `rs/sns/governance/src/governance.rs` L785–801). During this entire window, developer neurons hold 100% of voting power and can self-adopt any proposal not explicitly blocked.

The exploit path is:
1. SNS is created; governance enters `PreInitializationSwap`.
2. Swap opens; participants transfer ICP.
3. Developer submits `ManageLedgerParameters { transfer_fee: <large value> }` via `manage_neuron → MakeProposal`.
4. Developer's neuron self-adopts (100% voting power); `perform_manage_ledger_parameters` executes and updates the SNS ledger fee.
5. Swap reaches `LIFECYCLE_COMMITTED`; finalization distributes SNS tokens — each basket-neuron transfer is charged the inflated fee, reducing every participant's net allocation.
6. Participants who have already committed ICP cannot withdraw; the developer's genesis allocation is unaffected.

The `PreInitializationSwap` mode is the system's explicit protection mechanism against developer manipulation during the swap window. The missing entry in the denylist is a concrete authorization bypass of that mechanism.

## Impact Explanation
This is a **High** severity finding matching: *"Significant SNS security impact with concrete user or protocol harm."* Swap participants suffer a direct, quantifiable reduction in SNS token allocations. The developer retains their full genesis allocation. The harm is irreversible once the swap commits, as participants cannot withdraw ICP after `LIFECYCLE_COMMITTED`. The impact scales with the number of participants and the magnitude of the fee increase.

## Likelihood Explanation
- The attack window spans the entire `PreInitializationSwap` period (SNS creation to swap finalization).
- No capability beyond holding developer neurons is required; the proposal path is a standard `manage_neuron → MakeProposal` ingress call.
- Developer neurons hold 100% of voting power during this window; no external collusion is needed.
- `ManageLedgerParameters` is fully implemented and callable on mainnet SNS instances.

## Recommendation
Add `NervousSystemFunction::manage_ledger_parameters()` to the `functions_disallowed_in_pre_initialization_swap()` vector in `rs/sns/governance/src/types.rs` L253–262. Additionally audit `AdvanceSnsTargetVersion` (Id = 15) and `UpgradeSnsToNextVersion` (Id = 7) for the same omission, as neither is currently present in the denylist.

## Proof of Concept
1. Deploy an SNS via NNS `CreateServiceNervousSystem`; governance enters `PreInitializationSwap`.
2. Open the swap (`LIFECYCLE_OPEN`); have test participants transfer ICP.
3. From a developer neuron, call `manage_neuron → MakeProposal` with `Action::ManageLedgerParameters { transfer_fee: Some(100_000_000) }`.
4. Verify the proposal is accepted (not rejected with `PreconditionFailed`) and executes, updating the SNS ledger fee.
5. Finalize the swap; observe that each basket-neuron SNS token transfer is charged the inflated fee, reducing participant allocations below the amount implied by the swap parameters.
6. Confirm developer genesis neurons are unaffected.

This is reproducible as a PocketIC integration test against the SNS governance canister with a mock ledger canister recording the fee update call.