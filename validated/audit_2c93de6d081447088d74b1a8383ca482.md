### Title
`functions_disallowed_in_pre_initialization_swap()` Incomplete Restriction List Allows `ManageLedgerParameters` and Other Actions During Token Swap - (File: `rs/sns/governance/src/types.rs`)

---

### Summary

The `functions_disallowed_in_pre_initialization_swap()` function in SNS Governance returns a hardcoded, incomplete list of disallowed proposal actions for the `PreInitializationSwap` mode. Several native actions added after the initial design — most critically `ManageLedgerParameters` (id=13), `ManageDappCanisterSettings` (id=14), `AdvanceSnsTargetVersion` (id=15), `RegisterExtension` (id=17), `ExecuteExtensionOperation` (id=18), `UpgradeExtension` (id=19), and `SetTopicsForCustomProposals` (id=16) — are absent from the denylist and therefore pass the restriction check, allowing them to be submitted and executed while the SNS token swap is live.

---

### Finding Description

`PreInitializationSwap` mode is the governance mode active while the SNS token swap is running. Its purpose is to prevent the SNS team from making unilateral changes that could harm swap participants. The enforcement gate is `proposal_action_is_allowed_in_pre_initialization_swap_or_err`, which converts the incoming `Action` to a `NervousSystemFunction` by ID and checks membership against the list returned by `functions_disallowed_in_pre_initialization_swap()`. [1](#0-0) 

The denylist contains exactly six entries (IDs 2, 3, 9, 10, 11, 12): [2](#0-1) 

The check then falls through to `Ok(())` for any action whose ID is not in that list: [3](#0-2) 

The full set of native actions defined in the codebase is: [4](#0-3) 

`ManageLedgerParameters` (id=13), `ManageDappCanisterSettings` (id=14), `AdvanceSnsTargetVersion` (id=15), `RegisterExtension` (id=17), `ExecuteExtensionOperation` (id=18), `UpgradeExtension` (id=19), and `SetTopicsForCustomProposals` (id=16) are all absent from the denylist. Because `MakeProposal` is explicitly allowed in `PreInitializationSwap` mode: [5](#0-4) 

any neuron holder can submit these proposal types, and if the SNS team controls sufficient voting power (which they typically do at launch via developer neurons), the proposals will be adopted and executed.

The `ManageLedgerParameters` action can modify `transfer_fee`, `token_name`, `token_symbol`, and `token_logo` on the SNS ledger: [6](#0-5) 

The `From<Action> for NervousSystemFunction` mapping confirms id=13 for `ManageLedgerParameters`: [6](#0-5) 

The unit tests for `PreInitializationSwap` mode only cover the six explicitly listed actions and do not test `ManageLedgerParameters` or the other newer actions: [7](#0-6) 

---

### Impact Explanation

**Impact: Medium-High.**

The most dangerous missing entry is `ManageLedgerParameters`. During an active swap, the SNS team could pass a `ManageLedgerParameters` proposal to set `transfer_fee` to an arbitrarily large value (up to the token's total supply). Swap participants who purchased SNS tokens would find their tokens effectively non-transferable after the swap concludes, constituting a targeted rug-pull of token liquidity. Token name and symbol can also be changed, enabling reputational manipulation of the asset investors just purchased. `AdvanceSnsTargetVersion` and `UpgradeExtension`/`RegisterExtension` could be used to alter the SNS upgrade trajectory or install extension logic during the sensitive swap window, potentially bypassing future governance protections.

---

### Likelihood Explanation

**Likelihood: Low.**

Exploitation requires the SNS team to control enough neuron voting power to pass a proposal during the swap window. At SNS launch, developer neurons typically hold a majority of voting power before community neurons are distributed via the swap. A malicious or compromised SNS team could exploit this window. The attack is not available to a fully unprivileged external user, but it is reachable by the SNS founding team acting through the normal `manage_neuron` → `MakeProposal` ingress path without any privileged key or admin access beyond their own neurons.

---

### Recommendation

Add all native actions that can materially affect token economics or canister control to `functions_disallowed_in_pre_initialization_swap()`. At minimum, add:

```rust
NervousSystemFunction::manage_ledger_parameters(),
NervousSystemFunction::manage_dapp_canister_settings(),
NervousSystemFunction::advance_sns_target_version(),
NervousSystemFunction::register_extension(),
NervousSystemFunction::upgrade_extension(),
NervousSystemFunction::execute_extension_operation(),
```

Alternatively, invert the logic to an allowlist (only `Motion`, `AddGenericNervousSystemFunction`, `RemoveGenericNervousSystemFunction`, and safe `ExecuteGenericNervousSystemFunction` calls are permitted), so that newly added native actions are denied by default until explicitly reviewed.

---

### Proof of Concept

1. An SNS is launched and enters `PreInitializationSwap` mode while the token swap is live.
2. The SNS founding team, holding developer neurons with majority voting power, submits via `manage_neuron` → `MakeProposal`:
   ```
   Action::ManageLedgerParameters(ManageLedgerParameters {
       transfer_fee: Some(u64::MAX),  // effectively infinite fee
       ..Default::default()
   })
   ```
3. `allows_manage_neuron_command_or_err` passes because `MakeProposal` is in the allowed list.
4. `allows_proposal_action_or_err` calls `proposal_action_is_allowed_in_pre_initialization_swap_or_err`. `NervousSystemFunction::from(Action::ManageLedgerParameters)` yields id=13. The denylist `[2, 3, 9, 10, 11, 12]` does not contain 13, so `Ok(())` is returned.
5. The proposal is adopted and executed. The SNS ledger's `transfer_fee` is now `u64::MAX`.
6. All swap participants who received SNS tokens cannot transfer them (every transfer would require paying a fee equal to the maximum `u64` value in SNS tokens), effectively locking their assets. [8](#0-7) [5](#0-4) [4](#0-3)

### Citations

**File:** rs/sns/governance/src/types.rs (L88-131)
```rust
#[repr(u64)]
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum NativeAction {
    /// Unspecified Action.
    Unspecified = 0,
    /// Motion Action.
    Motion = 1,
    /// ManageNervousSystemParameters Action.
    ManageNervousSystemParameters = 2,
    /// UpgradeSnsControlledCanister Action.
    UpgradeSnsControlledCanister = 3,
    /// AddGenericNervousSystemFunction Action.
    AddGenericNervousSystemFunction = 4,
    /// RemoveGenericNervousSystemFunction Action.
    RemoveGenericNervousSystemFunction = 5,
    /// ExecuteGenericNervousSystemFunction Action.
    ExecuteGenericNervousSystemFunction = 6,
    /// UpgradeSnsToNextVersion Action.
    UpgradeSnsToNextVersion = 7,
    /// ManageSnsMetadata Action.
    ManageSnsMetadata = 8,
    /// TransferSnsTreasuryFunds Action.
    TransferSnsTreasuryFunds = 9,
    /// RegisterDappCanisters Action.
    RegisterDappCanisters = 10,
    /// DeregisterDappCanisters Action.
    DeregisterDappCanisters = 11,
    /// MintSnsTokens Action.
    MintSnsTokens = 12,
    /// ManageLedgerParameters Action.
    ManageLedgerParameters = 13,
    /// ManageDappCanisterSettings Action.
    ManageDappCanisterSettings = 14,
    /// AdvanceSnsTargetVersion Action.
    AdvanceSnsTargetVersion = 15,
    /// SetTopicsForCustomProposals Action.
    SetTopicsForCustomProposals = 16,
    /// RegisterExtension Action.
    RegisterExtension = 17,
    /// ExecuteExtensionOperation Action.
    ExecuteExtensionOperation = 18,
    /// UpgradeExtension Action.
    UpgradeExtension = 19,
}
```

**File:** rs/sns/governance/src/types.rs (L186-197)
```rust
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

**File:** rs/sns/governance/src/types.rs (L253-298)
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

    fn proposal_action_is_allowed_in_pre_initialization_swap_or_err(
        action: &Action,
        disallowed_target_canister_ids: &HashSet<CanisterId>,
        id_to_nervous_system_function: &BTreeMap<u64, NervousSystemFunction>,
    ) -> Result<(), GovernanceError> {
        // ExecuteGenericNervousSystemFunction is special in that it
        // is only disallowed in some cases.
        if let Action::ExecuteGenericNervousSystemFunction(execute) = action {
            return Self::execute_generic_nervous_system_function_is_allowed_in_pre_initialization_swap_or_err(
                    execute,
                    disallowed_target_canister_ids,
                    id_to_nervous_system_function,
                );
        }

        let nervous_system_function = NervousSystemFunction::from(action.clone());

        let is_action_disallowed = Self::functions_disallowed_in_pre_initialization_swap()
            .into_iter()
            .any(|t| t.id == nervous_system_function.id);

        if is_action_disallowed {
            Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Proposal type for {:?} is not allowed while governance is in \
                     PreInitializationSwap ({}) mode.",
                    nervous_system_function,
                    Mode::PreInitializationSwap as i32,
                ),
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/types.rs (L1401-1401)
```rust
            Action::ManageLedgerParameters(_) => NervousSystemFunction::manage_ledger_parameters(),
```

**File:** rs/sns/governance/src/types/tests.rs (L407-414)
```rust
        let disallowed_in_pre_initialization_swap = vec! [
            Action::ManageNervousSystemParameters(Default::default()),
            Action::TransferSnsTreasuryFunds(Default::default()),
            Action::MintSnsTokens(Default::default()),
            Action::UpgradeSnsControlledCanister(Default::default()),
            Action::RegisterDappCanisters(Default::default()),
            Action::DeregisterDappCanisters(Default::default()),
        ];
```
