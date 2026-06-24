### Title
Incomplete `PreInitializationSwap` Mode Blacklist Allows Restricted Governance Actions During SNS Token Swap — (File: `rs/sns/governance/src/types.rs`)

### Summary

The SNS Governance canister uses a single-value `Mode` enum to restrict operations during the initial token swap (`PreInitializationSwap`). The proposal-action gate uses a **blacklist** that was not updated when new native functions were added, allowing governance neuron holders to submit and pass proposals executing operations (e.g., `advance_sns_target_version`, `manage_ledger_parameters`, `manage_dapp_canister_settings`) that should be restricted during the swap. This is compounded by an inconsistency: the neuron-command gate uses a **whitelist** (safer), while the proposal-action gate uses a blacklist (unsafe by default for new additions).

### Finding Description

`governance::Mode` is a single-value enum with three variants: [1](#0-0) 

The `PreInitializationSwap` mode is intended to protect the integrity of the initial token swap. Two separate gating functions enforce this mode, but they use opposite approaches:

**Gate 1 — neuron commands (whitelist, safe):** Only explicitly listed commands are allowed. [2](#0-1) 

**Gate 2 — proposal actions (blacklist, unsafe):** Only explicitly listed functions are blocked. [3](#0-2) 

The blacklist contains only 6 functions. However, the full list of native functions registered in the system contains at least 7 additional entries not present in the blacklist: [4](#0-3) 

Functions **absent from the blacklist** but present as native actions include:
- `manage_ledger_parameters` — can alter ledger transaction fees mid-swap
- `manage_dapp_canister_settings` — can alter dapp canister settings
- `advance_sns_target_version` — can trigger an SNS canister upgrade during the swap
- `set_topics_for_custom_proposals`
- `register_extension`, `execute_extension_operation`, `upgrade_extension`

Both gates are invoked in the respective entry points: [5](#0-4) [6](#0-5) 

Because `MakeProposal` is explicitly whitelisted in Gate 1, any neuron holder can submit proposals during `PreInitializationSwap`. Gate 2 then fails to block proposals for the unlisted native functions.

### Impact Explanation

A governance neuron holder can submit and pass a proposal during the `PreInitializationSwap` phase to execute `advance_sns_target_version`, which triggers an SNS canister upgrade mid-swap, or `manage_ledger_parameters`, which changes the ledger transaction fee affecting swap economics. Either action can disrupt the integrity of the decentralization swap — the exact scenario `PreInitializationSwap` mode was designed to prevent. In the worst case, an upgrade during the swap could alter canister behavior in ways that harm swap participants or allow fund manipulation.

### Likelihood Explanation

During `PreInitializationSwap`, governance neurons already exist (created at SNS initialization). Any neuron holder with sufficient voting power — or who can coordinate with others — can submit and pass such a proposal. `MakeProposal` is explicitly whitelisted, so the submission path is fully open. The blacklist gap is a structural omission that grows with each new native function added without updating the list.

### Recommendation

- Replace the blacklist in `proposal_action_is_allowed_in_pre_initialization_swap_or_err` with a **whitelist**, mirroring the approach used for neuron commands. Only explicitly permitted proposal actions should be allowed during `PreInitializationSwap`.
- Add `advance_sns_target_version`, `manage_ledger_parameters`, `manage_dapp_canister_settings`, and all extension-related functions to the disallowed list as an immediate mitigation.
- Add a comment in `native_action_ids::nervous_system_functions` requiring that any new native function be reviewed for `PreInitializationSwap` compatibility.

### Proof of Concept

1. An SNS is deployed; the swap is in `PreInitializationSwap` mode.
2. A governance neuron holder calls `manage_neuron` with `MakeProposal` containing `Action::AdvanceSnsTargetVersion(...)`.
3. Gate 1 (`allows_manage_neuron_command_or_err`) passes — `MakeProposal` is whitelisted.
4. Gate 2 (`allows_proposal_action_or_err`) passes — `AdvanceSnsTargetVersion` is not in `functions_disallowed_in_pre_initialization_swap`.
5. The proposal is submitted and, if it reaches quorum, executed — triggering an SNS upgrade during the active token swap. [3](#0-2) [4](#0-3)

### Citations

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1591-1601)
```text
  enum Mode {
    // This forces people to explicitly populate the mode field.
    MODE_UNSPECIFIED = 0;

    // All operations are allowed.
    MODE_NORMAL = 1;

    // In this mode, various operations are not allowed in order to ensure the
    // integrity of the initial token swap.
    MODE_PRE_INITIALIZATION_SWAP = 2;
  }
```

**File:** rs/sns/governance/src/types.rs (L138-160)
```rust
    pub fn nervous_system_functions() -> Vec<NervousSystemFunction> {
        vec![
            NervousSystemFunction::motion(),
            NervousSystemFunction::manage_nervous_system_parameters(),
            NervousSystemFunction::upgrade_sns_controlled_canister(),
            NervousSystemFunction::add_generic_nervous_system_function(),
            NervousSystemFunction::remove_generic_nervous_system_function(),
            NervousSystemFunction::execute_generic_nervous_system_function(),
            NervousSystemFunction::upgrade_sns_to_next_version(),
            NervousSystemFunction::manage_sns_metadata(),
            NervousSystemFunction::transfer_sns_treasury_funds(),
            NervousSystemFunction::register_dapp_canisters(),
            NervousSystemFunction::deregister_dapp_canisters(),
            NervousSystemFunction::mint_sns_tokens(),
            NervousSystemFunction::manage_ledger_parameters(),
            NervousSystemFunction::manage_dapp_canister_settings(),
            NervousSystemFunction::advance_sns_target_version(),
            NervousSystemFunction::set_topics_for_custom_proposals(),
            NervousSystemFunction::register_extension(),
            NervousSystemFunction::execute_extension_operation(),
            NervousSystemFunction::upgrade_extension(),
        ]
    }
```

**File:** rs/sns/governance/src/types.rs (L182-197)
```rust
    fn manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err(
        command: &manage_neuron::Command,
        caller_is_swap_canister: bool,
    ) -> Result<(), GovernanceError> {
        use manage_neuron::Command as C;
        let ok = match command {
            C::Follow(_)
            | C::MakeProposal(_)
            | C::RegisterVote(_)
            | C::AddNeuronPermissions(_)
            | C::RemoveNeuronPermissions(_) => true,

            C::ClaimOrRefresh(_) => caller_is_swap_canister,

            _ => false,
        };
```

**File:** rs/sns/governance/src/types.rs (L253-262)
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

**File:** rs/sns/governance/src/governance.rs (L3483-3487)
```rust
        self.mode().allows_proposal_action_or_err(
            action,
            &disallowed_target_canister_ids,
            &self.proto.id_to_nervous_system_functions,
        )?;
```

**File:** rs/sns/governance/src/governance.rs (L4781-4782)
```rust
        self.mode()
            .allows_manage_neuron_command_or_err(command, self.is_swap_canister(*caller))?;
```
