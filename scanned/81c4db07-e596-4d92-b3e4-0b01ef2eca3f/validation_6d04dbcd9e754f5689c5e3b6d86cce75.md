### Title
Unbounded `transfer_fee` in SNS `ManageLedgerParameters` Proposal Allows Ledger to Be Rendered Unusable - (File: rs/sns/governance/src/proposal.rs)

### Summary
The `validate_and_render_manage_ledger_parameters` function in the SNS governance canister accepts any `u64` value for `transfer_fee` without any logical bounds check. A governance proposal setting this fee to an arbitrarily large value (e.g., `u64::MAX`) would make all SNS ledger transfers permanently impossible, bricking the SNS token economy. Setting it to `0` silently eliminates all fee collection. This is a direct analog to the `changeOriginationFeeRate` finding in the report.

### Finding Description

`validate_and_render_manage_ledger_parameters` in `rs/sns/governance/src/proposal.rs` is the proposal-time validation function for the `ManageLedgerParameters` action. When `transfer_fee` is `Some(value)`, the function only records the value for rendering — it performs **no validation of the value itself**:

```rust
if let Some(transfer_fee) = transfer_fee {
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
    change = true;
}
``` [1](#0-0) 

Compare this to `token_name`, `token_symbol`, and `token_logo`, which each call dedicated validation functions (`ledger_validation::validate_token_name`, etc.). The `transfer_fee` field has no equivalent guard. [2](#0-1) 

The `ManageLedgerParameters` struct itself is a plain `Option<u64>` with no type-level constraint: [3](#0-2) 

Upon successful proposal execution, the SNS governance canister also updates `NervousSystemParameters.transaction_fee_e8s` to match the new ledger fee: [4](#0-3) 

The `NervousSystemParameters` validation only checks `neuron_minimum_stake_e8s > transaction_fee_e8s` — it does not enforce any absolute upper bound on `transaction_fee_e8s`: [5](#0-4) 

### Impact Explanation

**Setting `transfer_fee` to `u64::MAX` (or any value exceeding all user balances):**
- All ICRC-1 ledger transfers fail with `InsufficientFunds` because no account can pay the fee.
- Neuron disbursement, staking, and splitting all require ledger transfers and become permanently impossible.
- The SNS token economy is bricked: tokens are locked in accounts and neurons with no way to move them.
- The `NervousSystemParameters.transaction_fee_e8s` is also updated to `u64::MAX`, causing the `neuron_minimum_stake_e8s > transaction_fee_e8s` invariant to be violated for all existing neurons, corrupting governance state.

**Setting `transfer_fee` to `0`:**
- All transfers become free; fee collection stops entirely, silently breaking any economic model that depends on fees.

Impact: **High** — the SNS ledger and governance token economy can be rendered permanently non-functional.

### Likelihood Explanation

Likelihood: **Low-to-Moderate**. Any SNS token holder with sufficient voting power can submit a `ManageLedgerParameters` proposal. The SNS governance community may not realize that no on-chain guard prevents a catastrophic fee value. A mistaken proposal (e.g., entering a fee in the wrong unit — e8s vs. tokens) or a malicious proposal that passes could trigger this. The absence of any validation means there is no safety net even for accidental misconfiguration.

### Recommendation

Add explicit bounds validation for `transfer_fee` inside `validate_and_render_manage_ledger_parameters`:

1. Reject `transfer_fee = 0` or define a minimum floor (e.g., `>= 1`).
2. Enforce a maximum ceiling (e.g., `<= some_reasonable_max_e8s`) to prevent the ledger from being bricked.
3. Optionally, cross-validate that the new `transfer_fee` is strictly less than the current `neuron_minimum_stake_e8s` to preserve the `neuron_minimum_stake_e8s > transaction_fee_e8s` invariant at proposal time, not just at parameter-update time.

### Proof of Concept

1. An SNS token holder submits a `ManageLedgerParameters` proposal with `transfer_fee: Some(u64::MAX)`.
2. `validate_and_render_manage_ledger_parameters` is called; it finds `transfer_fee` is `Some`, appends a render string, sets `change = true`, and returns `Ok(render)` — no value check occurs. [1](#0-0) 
3. The proposal passes governance voting and executes. The SNS ledger is upgraded with `transfer_fee = u64::MAX`.
4. `nervous_system_parameters.transaction_fee_e8s` is set to `u64::MAX`. [4](#0-3) 
5. Every subsequent ICRC-1 transfer attempt fails: `BadFee { expected_fee: u64::MAX }` or `InsufficientFunds`. Neuron disbursement, staking, and splitting are all permanently blocked. The SNS is non-functional.

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

**File:** rs/sns/governance/src/governance.rs (L3191-3195)
```rust
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
```

**File:** rs/sns/governance/src/types.rs (L620-624)
```rust
    /// Validates that the nervous system parameter transaction_fee_e8s is well-formed.
    fn validate_transaction_fee_e8s(&self) -> Result<u64, String> {
        self.transaction_fee_e8s
            .ok_or_else(|| "NervousSystemParameters.transaction_fee_e8s must be set".to_string())
    }
```
