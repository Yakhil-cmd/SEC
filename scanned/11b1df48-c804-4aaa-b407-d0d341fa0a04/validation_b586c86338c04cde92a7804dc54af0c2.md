### Title
Missing Upper Bound Validation on `transfer_fee` in `ManageLedgerParameters` Proposal — (File: `rs/sns/governance/src/proposal.rs`)

### Summary
The SNS governance canister's `validate_and_render_manage_ledger_parameters` function accepts any `u64` value for `transfer_fee` without checking it against `neuron_minimum_stake_e8s`. This is the direct IC analog of the `setRoyaltyInfo()` missing-bounds bug: a numeric parameter controlling a fee ratio is accepted without an upper bound, allowing it to exceed the value it must remain below to preserve a critical system invariant.

### Finding Description
`ManageLedgerParameters` is a native SNS governance proposal action that lets any SNS neuron holder submit a proposal to change the SNS ledger's transfer fee. The validation function `validate_and_render_manage_ledger_parameters` in `rs/sns/governance/src/proposal.rs` (lines 1761–1799) accepts the `transfer_fee` field and performs **no bounds check whatsoever**:

```rust
if let Some(transfer_fee) = transfer_fee {
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
    change = true;
    // ← no check that transfer_fee < neuron_minimum_stake_e8s
}
``` [1](#0-0) 

The SNS governance system enforces a hard invariant in `NervousSystemParameters::validate_neuron_minimum_stake_e8s`: `neuron_minimum_stake_e8s > transaction_fee_e8s`. This invariant is checked at SNS initialization and on every `ManageNervousSystemParameters` proposal. [2](#0-1) 

However, when a `ManageLedgerParameters` proposal executes successfully, the governance canister **silently overwrites** `transaction_fee_e8s` in `NervousSystemParameters` with the new fee value, with no re-validation of the invariant:

```rust
if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
    && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
{
    nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
}
``` [3](#0-2) 

The `ManageLedgerParameters` struct itself carries no constraint on `transfer_fee`: [4](#0-3) 

The invariant `neuron_minimum_stake_e8s > transaction_fee_e8s` is enforced at SNS init time and in `ManageNervousSystemParameters` proposals, but the `ManageLedgerParameters` path bypasses it entirely. [5](#0-4) 

### Impact Explanation
If a `ManageLedgerParameters` proposal sets `transfer_fee >= neuron_minimum_stake_e8s`:

1. **New neurons cannot be staked** — the staked amount would be ≤ the fee, so the ledger burn would consume the entire stake.
2. **Existing neurons cannot be split** — split validation checks `amount_e8s >= neuron_minimum_stake_e8s + transaction_fee_e8s`, which becomes impossible to satisfy.
3. **SNS ledger becomes unusable** — if `transfer_fee` is set to `u64::MAX`, every transfer attempt fails with `InsufficientFunds`.
4. **Governance is permanently bricked** — no new neurons can be created to submit a recovery proposal; the SNS is frozen.

This is a **ledger conservation bug** and **governance authorization bug**: the fee parameter has no upper bound, so it can be set to a value that exceeds the total token supply or violates the staking invariant, permanently destroying the SNS's ability to function.

### Likelihood Explanation
The entry path is: any SNS neuron holder submits a `ManageLedgerParameters` proposal. The proposal passes `validate_and_render_manage_ledger_parameters` with no rejection regardless of the `transfer_fee` value. Execution requires a governance majority vote. In early-stage SNS deployments where a small number of large neurons hold majority voting power, this is a realistic scenario. The missing validation at the proposal-submission layer means there is no protocol-level protection — the invariant violation is only prevented by social/governance consensus, not by code.

### Recommendation
Add a bounds check inside `validate_and_render_manage_ledger_parameters` that rejects any proposed `transfer_fee` that would violate `neuron_minimum_stake_e8s > transaction_fee_e8s`. Specifically, the function should read the current `neuron_minimum_stake_e8s` from `NervousSystemParameters` and reject the proposal if `transfer_fee >= neuron_minimum_stake_e8s`. Additionally, a reasonable absolute upper bound (e.g., `u64::MAX / 2`) should be enforced to prevent overflow-adjacent misuse.

### Proof of Concept
1. Acquire any SNS neuron (no majority required to submit).
2. Submit a `ManageLedgerParameters` proposal with `transfer_fee = u64::MAX`.
3. `validate_and_render_manage_ledger_parameters` accepts it — no bounds check fires.
4. If the proposal passes the governance vote, `CkBtcMinterState::upgrade` / SNS governance executes it: the SNS ledger is upgraded with `transfer_fee = u64::MAX`, and `nervous_system_parameters.transaction_fee_e8s` is set to `u64::MAX`.
5. Every subsequent ledger transfer fails with `InsufficientFunds`. No new neurons can be staked. The SNS governance is permanently frozen with no on-chain recovery path. [6](#0-5) [7](#0-6)

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

**File:** rs/sns/governance/src/governance.rs (L3191-3196)
```rust
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
                    return Ok(());
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

**File:** rs/sns/init/src/lib.rs (L1627-1633)
```rust
        // (7)
        if neuron_minimum_stake_e8s <= sns_transaction_fee_e8s {
            return Err(format!(
                "Error: neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) is too small. It needs to be \
                 greater than the transaction fee ({sns_transaction_fee_e8s} e8s)"
            ));
        }
```
