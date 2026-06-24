### Title
Unconstrained `transfer_fee` in `ManageLedgerParameters` Proposal Breaks `neuron_minimum_stake_e8s > transaction_fee_e8s` Invariant - (File: rs/sns/governance/src/proposal.rs)

### Summary

The SNS governance canister allows a `ManageLedgerParameters` proposal to set `transfer_fee` to an arbitrarily large value — including one that equals or exceeds `neuron_minimum_stake_e8s` — without any bounds validation. This breaks a critical invariant that the codebase itself documents and enforces in every other code path, and can render the SNS ledger and governance system permanently non-functional for all token holders.

### Finding Description

The `validate_and_render_manage_ledger_parameters` function in `rs/sns/governance/src/proposal.rs` performs no numeric validation on the `transfer_fee` field. When `transfer_fee` is `Some(value)`, the function only formats a render string and sets `change = true`:

```rust
if let Some(transfer_fee) = transfer_fee {
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
    change = true;
}
``` [1](#0-0) 

No check is made against the current `neuron_minimum_stake_e8s`. By contrast, `NervousSystemParameters::validate()` — called on every `ManageNervousSystemParameters` proposal — explicitly enforces:

```rust
if neuron_minimum_stake_e8s <= transaction_fee_e8s {
    Err(...)
}
``` [2](#0-1) 

When the proposal executes, `perform_manage_ledger_parameters` directly writes the new fee into `nervous_system_parameters.transaction_fee_e8s` without re-running any invariant check:

```rust
nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
``` [3](#0-2) 

The invariant `neuron_minimum_stake_e8s > transaction_fee_e8s` is documented in the proto and enforced at SNS init time and via `ManageNervousSystemParameters`, but the `ManageLedgerParameters` execution path bypasses it entirely. [4](#0-3) 

### Impact Explanation

If `transfer_fee` is set to a value `>= neuron_minimum_stake_e8s`:

1. **Neuron staking breaks**: Every ledger transfer to stake a neuron will fail because the fee consumes the entire stake amount.
2. **Neuron disbursement breaks**: Existing neuron holders cannot disburse their stake — the ledger transfer fails because the fee exceeds the disbursement amount.
3. **Neuron splitting breaks**: Split operations require a ledger transfer; all will fail.
4. **Governance becomes stuck**: Since governance operations (voting, claiming neurons) depend on ledger transfers, the SNS governance canister can become permanently non-functional.
5. **The `NervousSystemParameters` state becomes inconsistent**: `transaction_fee_e8s` in governance state will exceed `neuron_minimum_stake_e8s`, violating the invariant that `validate()` is supposed to guarantee.

This is a ledger conservation / governance authorization bug with high impact: permanent loss of user funds locked in neurons and potential bricking of the entire SNS.

### Likelihood Explanation

The `ManageLedgerParameters` action is a standard SNS governance proposal type, reachable by any SNS token holder with sufficient voting power. A single large token holder, or a coordinated group, can submit and pass such a proposal. Even a well-intentioned governance majority could accidentally set a fee that violates the invariant, since the proposal validation gives no warning. The missing check is a straightforward code omission with no compensating control.

### Recommendation

In `validate_and_render_manage_ledger_parameters`, when `transfer_fee` is `Some(value)`, validate that the new fee is strictly less than the current `neuron_minimum_stake_e8s` from `NervousSystemParameters`. The function should accept the current parameters as an argument (similar to `validate_and_render_manage_nervous_system_parameters`) and reject proposals where `transfer_fee >= neuron_minimum_stake_e8s`. Additionally, in `perform_manage_ledger_parameters`, after updating `transaction_fee_e8s`, re-run `NervousSystemParameters::validate()` as a defense-in-depth check.

### Proof of Concept

**Entry path**: Submit a `ManageLedgerParameters` proposal via SNS governance with `transfer_fee = neuron_minimum_stake_e8s` (e.g., `100_000_000` e8s if the minimum stake is `1 ICP`).

**Validation gap**: `validate_and_render_manage_ledger_parameters` accepts any `u64` value for `transfer_fee` without checking it against `neuron_minimum_stake_e8s`: [5](#0-4) 

**Execution**: After the proposal passes, `perform_manage_ledger_parameters` sets `transaction_fee_e8s = transfer_fee` unconditionally: [3](#0-2) 

**Broken invariant**: `NervousSystemParameters` now has `transaction_fee_e8s >= neuron_minimum_stake_e8s`, which `validate()` would reject if called — but it is never called in this code path: [6](#0-5) 

**Result**: All subsequent ledger transfers for neuron operations fail. The SNS is bricked for all token holders. The `ManageLedgerParameters` struct and its proposal action are defined here: [7](#0-6)

### Citations

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

**File:** rs/sns/governance/src/types.rs (L570-594)
```rust
    /// This validates that the `NervousSystemParameters` are well-formed.
    pub fn validate(&self) -> Result<(), String> {
        self.validate_reject_cost_e8s()?;
        self.validate_neuron_minimum_stake_e8s()?;
        self.validate_transaction_fee_e8s()?;
        self.validate_max_proposals_to_keep_per_action()?;
        self.validate_initial_voting_period_seconds()?;
        self.validate_wait_for_quiet_deadline_increase_seconds()?;
        self.validate_default_followees()?;
        self.validate_max_number_of_neurons()?;
        self.validate_neuron_minimum_dissolve_delay_to_vote_seconds()?;
        self.validate_max_followees_per_function()?;
        self.validate_max_dissolve_delay_seconds()?;
        self.validate_max_neuron_age_for_age_bonus()?;
        self.validate_max_number_of_proposals_with_ballots()?;
        self.validate_neuron_claimer_permissions()?;
        self.validate_neuron_grantable_permissions()?;
        self.validate_max_number_of_principals_per_neuron()?;
        self.validate_voting_rewards_parameters()?;
        self.validate_max_dissolve_delay_bonus_percentage()?;
        self.validate_max_age_bonus_percentage()?;
        self.validate_additional_critical_native_action_ids()?;

        Ok(())
    }
```

**File:** rs/sns/governance/src/types.rs (L602-618)
```rust
    /// Validates that the nervous system parameter neuron_minimum_stake_e8s is well-formed.
    fn validate_neuron_minimum_stake_e8s(&self) -> Result<(), String> {
        let transaction_fee_e8s = self.validate_transaction_fee_e8s()?;

        let neuron_minimum_stake_e8s = self.neuron_minimum_stake_e8s.ok_or_else(|| {
            "NervousSystemParameters.neuron_minimum_stake_e8s must be set".to_string()
        })?;

        if neuron_minimum_stake_e8s <= transaction_fee_e8s {
            Err(format!(
                "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
                NervousSystemParameters.transaction_fee_e8s ({neuron_minimum_stake_e8s})"
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L3191-3195)
```rust
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L558-567)
```rust
pub struct ManageLedgerParameters {
    #[prost(uint64, optional, tag = "1")]
    pub transfer_fee: ::core::option::Option<u64>,
    #[prost(string, optional, tag = "2")]
    pub token_name: ::core::option::Option<::prost::alloc::string::String>,
    #[prost(string, optional, tag = "3")]
    pub token_symbol: ::core::option::Option<::prost::alloc::string::String>,
    #[prost(string, optional, tag = "4")]
    pub token_logo: ::core::option::Option<::prost::alloc::string::String>,
}
```
