### Title
Missing Transfer Fee Validation in `ManageLedgerParameters` Allows Setting Fee ≥ `neuron_minimum_stake_e8s`, Breaking Neuron Staking Invariant - (`File: rs/sns/governance/src/proposal.rs`)

### Summary
The `validate_and_render_manage_ledger_parameters` function in the SNS governance canister accepts any `transfer_fee` value — including values equal to or greater than the current `neuron_minimum_stake_e8s` — without checking the critical invariant that `neuron_minimum_stake_e8s > transaction_fee_e8s`. Once such a `ManageLedgerParameters` proposal is adopted and executed, the SNS ledger's transfer fee is updated and `transaction_fee_e8s` in `NervousSystemParameters` is silently overwritten to the new (invalid) value. This permanently breaks neuron staking, splitting, and disbursing for all SNS participants.

### Finding Description
The `validate_and_render_manage_ledger_parameters` function validates `token_name`, `token_symbol`, and `token_logo` fields but performs **no validation whatsoever on the `transfer_fee` value**:

```rust
// rs/sns/governance/src/proposal.rs:1773-1776
if let Some(transfer_fee) = transfer_fee {
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
    change = true;
    // ← No check that transfer_fee < neuron_minimum_stake_e8s
}
```

The `NervousSystemParameters::validate_neuron_minimum_stake_e8s` enforces the invariant `neuron_minimum_stake_e8s > transaction_fee_e8s`:

```rust
// rs/sns/governance/src/types.rs:610-614
if neuron_minimum_stake_e8s <= transaction_fee_e8s {
    Err(format!("...must be greater than transaction_fee_e8s..."))
}
```

However, this invariant is **never checked** when a `ManageLedgerParameters` proposal is validated. After execution, `perform_manage_ledger_parameters` silently overwrites `transaction_fee_e8s` in `NervousSystemParameters` with the new fee:

```rust
// rs/sns/governance/src/governance.rs:3191-3194
if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
    && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
{
    nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
}
```

This is the exact analog of the reported Popcorn bug: a configuration parameter (the ledger transfer fee) is accepted without validity checks against a dependent parameter (`neuron_minimum_stake_e8s`), and once applied, it permanently breaks downstream operations for all users.

### Impact Explanation
Once a `ManageLedgerParameters` proposal with `transfer_fee >= neuron_minimum_stake_e8s` is executed:

1. **Neuron staking breaks**: `claim_or_refresh_neuron` and `split` both check `amount >= min_stake + transaction_fee_e8s`. With `transaction_fee_e8s >= neuron_minimum_stake_e8s`, this sum overflows or is always too large, making it impossible to stake new neurons or split existing ones.
2. **Neuron disbursing breaks**: `disburse_maturity` and related operations that check against `transaction_fee_e8s` will reject all disbursements below the now-inflated fee.
3. **The SNS ledger itself becomes unusable** for token transfers if `transfer_fee` is set to an absurdly large value, since all transfers require paying the fee.
4. **The broken state is permanent**: there is no recovery path without a subsequent governance proposal — but if the fee is set so high that no one can stake neurons or pay proposal fees, the SNS governance itself may become unable to pass further proposals.

This is a **governance authorization bug / cycles-resource accounting bug** that causes permanent loss of SNS token utility and neuron functionality for all participants.

### Likelihood Explanation
The attack path is reachable by any SNS neuron holder with sufficient voting power to pass a `ManageLedgerParameters` proposal. This is a standard governance action available to any SNS community member. A malicious or careless neuron holder can submit such a proposal with `transfer_fee = u64::MAX` or any value `>= neuron_minimum_stake_e8s`. The proposal passes through `validate_and_render_manage_ledger_parameters` without rejection. No privileged access, admin key, or threshold corruption is required — only a governance majority, which is the normal operating condition for SNS proposals.

### Recommendation
In `validate_and_render_manage_ledger_parameters`, add a cross-field validity check when `transfer_fee` is set:

1. Retrieve the current `neuron_minimum_stake_e8s` from `NervousSystemParameters` and reject the proposal if `transfer_fee >= neuron_minimum_stake_e8s`.
2. Alternatively, pass the current `NervousSystemParameters` into the validation function and call `NervousSystemParameters::validate()` on the hypothetical merged state (i.e., with `transaction_fee_e8s` replaced by the proposed `transfer_fee`).
3. At minimum, reject `transfer_fee == 0` as a sanity check.

### Proof of Concept

1. An SNS community member submits a `ManageLedgerParameters` proposal with `transfer_fee = Some(u64::MAX)` (or any value `>= neuron_minimum_stake_e8s`).

2. `validate_and_render_manage_ledger_parameters` is called during proposal submission. It only checks that `transfer_fee` is `Some` and renders a string — no range or cross-field check is performed: [1](#0-0) 

3. The proposal is adopted and `perform_manage_ledger_parameters` executes, upgrading the SNS ledger with the new fee and then overwriting `transaction_fee_e8s` in `NervousSystemParameters`: [2](#0-1) 

4. From this point, `NervousSystemParameters::validate_neuron_minimum_stake_e8s` would return an error if called, because `neuron_minimum_stake_e8s <= transaction_fee_e8s`: [3](#0-2) 

5. All subsequent neuron `split` calls fail because `split.amount_e8s < min_stake + transaction_fee_e8s` is always true when `transaction_fee_e8s` is enormous: [4](#0-3) 

6. All SNS token transfers on the ledger require paying the now-enormous fee, making the SNS token economically unusable. The SNS is permanently degraded with no automatic recovery.

The root cause — missing cross-field validation on `transfer_fee` in `validate_and_render_manage_ledger_parameters` — is a direct analog of the Popcorn escrow config bug: a configuration parameter is accepted without checking its validity against dependent parameters, and once applied, it permanently breaks downstream operations for all users. [5](#0-4)

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

**File:** rs/sns/governance/src/governance.rs (L1318-1330)
```rust
        if split.amount_e8s < min_stake + transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split a neuron with argument {} e8s. This is too little: \
                      at the minimum, one needs the minimum neuron stake, which is {} e8s, \
                      plus the transaction fee, which is {}. Hence the minimum split amount is {}.",
                    split.amount_e8s,
                    min_stake,
                    transaction_fee_e8s,
                    min_stake + transaction_fee_e8s
                ),
            ));
```

**File:** rs/sns/governance/src/governance.rs (L3190-3195)
```rust
                    // update nervous-system-parameters transaction_fee if the fee is changed.
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
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
