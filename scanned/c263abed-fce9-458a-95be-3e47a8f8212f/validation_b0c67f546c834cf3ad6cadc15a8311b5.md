### Title
`ManageLedgerParameters` Proposals Not Restricted During `PreInitializationSwap` Mode — (`File: rs/sns/governance/src/types.rs`)

---

### Summary

The SNS Governance canister defines a `PreInitializationSwap` mode specifically to protect the integrity of the decentralization swap. While several sensitive proposal actions are blocked in this mode, `ManageLedgerParameters` is absent from the disallowed list. This allows SNS neuron holders with sufficient voting power to change the SNS token's transfer fee, name, symbol, or logo while the swap is actively running — directly analogous to the crowdsale bug where `setFundingCap`, `setPricingStrategy`, and `setEndsAt` could be called mid-sale.

---

### Finding Description

The `functions_disallowed_in_pre_initialization_swap()` function in `rs/sns/governance/src/types.rs` enumerates the proposal actions blocked during the swap lifecycle: [1](#0-0) 

The list includes `ManageNervousSystemParameters`, `TransferSnsTreasuryFunds`, `MintSnsTokens`, `UpgradeSnsControlledCanister`, `RegisterDappCanisters`, and `DeregisterDappCanisters`. **`ManageLedgerParameters` is not present.**

The gating logic in `proposal_action_is_allowed_in_pre_initialization_swap_or_err` uses a simple allowlist-by-exclusion: any action whose ID is not in the disallowed list is permitted: [2](#0-1) 

`ManageLedgerParameters` proposals can change:
- `transfer_fee` — the per-transaction fee on the SNS ledger
- `token_name` — the human-readable name of the token
- `token_symbol` — the ticker symbol
- `token_logo` — the visual identity [3](#0-2) 

The validation function for `ManageLedgerParameters` performs no lifecycle check whatsoever. The test suite for `PreInitializationSwap` mode confirms `ManageLedgerParameters` is absent from both the allowed and disallowed lists, meaning it falls through as permitted: [4](#0-3) 

This is inconsistent with the stated purpose of `PreInitializationSwap` mode: [5](#0-4) 

Notably, `ManageNervousSystemParameters` IS blocked, and it also carries a `transaction_fee_e8s` field — yet the ledger's `transfer_fee` can be changed via `ManageLedgerParameters` during the same period. This creates an inconsistency where one path to changing the fee is blocked and another is not.

---

### Impact Explanation

**Vulnerability class: Governance authorization bug.**

An SNS neuron holder (or coalition) with sufficient voting power can, while the decentralization swap is in `LIFECYCLE_OPEN` or `LIFECYCLE_ADOPTED` state:

1. **Change `transfer_fee`**: Alter the economics of the token that swap participants are purchasing. Participants who evaluated the token based on a specific fee structure find the rules changed mid-swap.
2. **Change `token_name` / `token_symbol`**: Rebrand the token mid-swap, potentially misleading participants about what they are buying. A participant who committed ICP to buy "ProjectX" tokens could find they receive tokens now named "ScamToken."
3. **Change `token_logo`**: Minor but contributes to deceptive rebranding.

The `PreInitializationSwap` mode is explicitly designed to prevent exactly this class of mid-swap parameter manipulation. The omission of `ManageLedgerParameters` from the disallowed list undermines the integrity guarantee the mode is meant to provide.

---

### Likelihood Explanation

During `PreInitializationSwap`, the SNS founding team typically holds the majority of voting power (developer neurons). A malicious or compromised founding team can unilaterally pass a `ManageLedgerParameters` proposal without needing external coordination. Any SNS neuron holder with a majority stake — or a coalition reaching quorum — can submit and execute this proposal via the standard `make_proposal` ingress path. No privileged system access, key compromise, or subnet-majority attack is required. The entry path is a standard canister update call to the SNS Governance canister's `manage_neuron` endpoint.

---

### Recommendation

Add `ManageLedgerParameters` to `functions_disallowed_in_pre_initialization_swap()`:

```rust
pub fn functions_disallowed_in_pre_initialization_swap() -> Vec<NervousSystemFunction> {
    vec![
        NervousSystemFunction::manage_nervous_system_parameters(),
        NervousSystemFunction::manage_ledger_parameters(), // ADD THIS
        NervousSystemFunction::transfer_sns_treasury_funds(),
        NervousSystemFunction::mint_sns_tokens(),
        NervousSystemFunction::upgrade_sns_controlled_canister(),
        NervousSystemFunction::register_dapp_canisters(),
        NervousSystemFunction::deregister_dapp_canisters(),
    ]
}
``` [1](#0-0) 

Also update the corresponding CLI confirmation message and the `functions_disallowed_in_pre_initialization_swap()` helper in the CLI: [6](#0-5) 

Add a test case to `rs/sns/governance/src/types/tests.rs` asserting that `Action::ManageLedgerParameters(Default::default())` is rejected in `PreInitializationSwap` mode, mirroring the existing pattern for the other disallowed actions.

---

### Proof of Concept

1. An SNS is created and enters `PreInitializationSwap` mode (swap lifecycle = `ADOPTED` or `OPEN`).
2. A founding-team neuron holder calls `manage_neuron` → `MakeProposal` with action `ManageLedgerParameters { token_name: Some("ScamToken".to_string()), token_symbol: Some("SCAM".to_string()), .. }`.
3. `allows_proposal_action_or_err` is called with `Mode::PreInitializationSwap`. The action is not `ExecuteGenericNervousSystemFunction`, so it is converted to a `NervousSystemFunction`. Its ID is not in `functions_disallowed_in_pre_initialization_swap()`, so the function returns `Ok(())`.
4. The proposal is submitted, voted on, and executed. The SNS ledger's token name and symbol are changed mid-swap.
5. Swap participants who committed ICP to buy "ProjectX" tokens now hold tokens named "ScamToken / SCAM." [7](#0-6) [3](#0-2)

### Citations

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

**File:** rs/sns/governance/src/types.rs (L264-298)
```rust
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

**File:** rs/sns/governance/src/proposal.rs (L1761-1799)
```rust
fn validate_and_render_manage_ledger_parameters(
    manage_ledger_parameters: &ManageLedgerParameters,
) -> Result<String, String> {
    let mut change = false;
    let mut render = "# Proposal to change ledger parameters:\n".to_string();
    let ManageLedgerParameters {
        transfer_fee,
        token_name,
        token_symbol,
        token_logo,
    } = manage_ledger_parameters;

    if let Some(transfer_fee) = transfer_fee {
        render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
        change = true;
    }
    if let Some(token_name) = token_name {
        ledger_validation::validate_token_name(token_name)?;
        render += &format!("# Set token name: {token_name}. \n",);
        change = true;
    }
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
        change = true;
    }
    if let Some(token_logo) = token_logo {
        ledger_validation::validate_token_logo(token_logo)?;
        render += &format!("# Set token logo: {token_logo}. \n",);
        change = true;
    }
    if !change {
        Err(String::from(
            "ManageLedgerParameters must change at least one value, all values are None",
        ))
    } else {
        Ok(render)
    }
}
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1598-1601)
```text
    // In this mode, various operations are not allowed in order to ensure the
    // integrity of the initial token swap.
    MODE_PRE_INITIALIZATION_SWAP = 2;
  }
```

**File:** rs/sns/cli/src/propose.rs (L172-181)
```rust
fn functions_disallowed_in_pre_initialization_swap() -> Vec<&'static str> {
    vec![
        "ManageNervousSystemParameters",
        "TransferSnsTreasuryFunds",
        "MintSnsTokens",
        "UpgradeSnsControlledCanister",
        "RegisterDappCanisters",
        "DeregisterDappCanisters",
    ]
}
```
