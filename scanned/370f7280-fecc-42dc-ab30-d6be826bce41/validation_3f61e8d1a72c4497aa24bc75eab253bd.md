### Title
Missing Upper Bound on `transfer_fee` in `ManageLedgerParameters` Breaks SNS Neuron Operation Invariants - (File: `rs/sns/governance/src/proposal.rs`)

### Summary
The `ManageLedgerParameters` SNS governance proposal action allows setting the SNS ledger's `transfer_fee` to an arbitrarily large value with no upper bound validation. This silently breaks the critical invariant `neuron_minimum_stake_e8s > transaction_fee_e8s`, causing integer overflow in neuron split/disburse arithmetic and bricking those operations for all SNS token holders.

### Finding Description
`validate_and_render_manage_ledger_parameters` in `rs/sns/governance/src/proposal.rs` validates a `ManageLedgerParameters` proposal but performs **no upper-bound check** on the proposed `transfer_fee`: [1](#0-0) 

When the proposal executes, `perform_manage_ledger_parameters` in `rs/sns/governance/src/governance.rs` unconditionally writes the new fee into `NervousSystemParameters` without re-validating the cross-parameter invariant: [2](#0-1) 

`validate_transaction_fee_e8s` in `rs/sns/governance/src/types.rs` only checks that the field is present — no ceiling is enforced: [3](#0-2) 

The invariant `neuron_minimum_stake_e8s > transaction_fee_e8s` is enforced only when a `ManageNervousSystemParameters` proposal is executed: [4](#0-3) 

It is **never re-checked** after `ManageLedgerParameters` updates `transaction_fee_e8s`. The two proposal paths are independent, so a `ManageLedgerParameters` proposal can silently violate the invariant that `ManageNervousSystemParameters` enforces.

### Impact Explanation
Once `transaction_fee_e8s >= neuron_minimum_stake_e8s`, two critical paths break:

**1. `split_neuron` — unchecked integer overflow** [5](#0-4) 

The expression `min_stake + transaction_fee_e8s` uses plain `+` with no overflow guard. In Rust release builds (as IC canisters are compiled), this wraps silently. If `transaction_fee_e8s` is set to `u64::MAX - min_stake + 1`, the sum wraps to 0, making the guard `split.amount_e8s < 0` always false and allowing any split amount — or, with other large values, the guard becomes an arbitrary small threshold, corrupting split logic entirely.

**2. `disburse_neuron` — fee exceeds neuron balance, bricking disbursal** [6](#0-5) 

If `transaction_fee_e8s` exceeds the neuron's stake, the guard `disburse_amount_e8s > transaction_fee_e8s` is false, so `disburse_amount_e8s` is not decremented. The subsequent ledger transfer is called with a fee that exceeds the neuron's account balance, causing the ledger to reject it. Every dissolved neuron becomes permanently undisburse-able.

**3. New neuron staking is blocked** because `neuron_minimum_stake_e8s` is now ≤ `transaction_fee_e8s`, violating the staking precondition enforced elsewhere.

The net effect is that all SNS token holders with staked neurons lose the ability to split or disburse — a complete availability break for the SNS governance token economy.

### Likelihood Explanation
`ManageLedgerParameters` is a standard SNS governance proposal type reachable by any SNS token holder who can assemble a voting majority. No special key or admin role is required beyond normal governance participation. The missing validation is a silent gap — there is no error, no warning, and no cross-check at proposal submission time. An accidental or adversarial proposal setting `transfer_fee` to a value ≥ `neuron_minimum_stake_e8s` (e.g., setting it to `u64::MAX` or simply to a value larger than the current minimum stake) passes all existing validation and executes successfully, leaving the SNS in a broken state with no on-chain recovery path short of another governance proposal.

### Recommendation
1. In `validate_and_render_manage_ledger_parameters`, add a check that the proposed `transfer_fee`, when applied, satisfies `transfer_fee < current_neuron_minimum_stake_e8s`. This requires passing `current_parameters` into the validator (as is already done for `validate_and_render_manage_nervous_system_parameters`).
2. In `perform_manage_ledger_parameters`, after updating `transaction_fee_e8s`, call `new_params.validate()` on the resulting `NervousSystemParameters` and return an error if the invariant is violated.
3. Replace the plain `+` in `split_neuron` and similar arithmetic with `checked_add`, returning a `GovernanceError` on overflow rather than silently wrapping.

### Proof of Concept
1. An SNS has `neuron_minimum_stake_e8s = 100_000_000` (1 token) and `transaction_fee_e8s = 10_000`.
2. A governance majority passes `ManageLedgerParameters { transfer_fee: Some(200_000_000), .. }`.
3. `validate_and_render_manage_ledger_parameters` accepts it — no upper-bound check exists.
4. `perform_manage_ledger_parameters` upgrades the ledger and then sets `nervous_system_parameters.transaction_fee_e8s = Some(200_000_000)`.
5. Now `transaction_fee_e8s (200_000_000) > neuron_minimum_stake_e8s (100_000_000)`.
6. Any call to `split_neuron` computes `min_stake + transaction_fee_e8s = 300_000_000`; since no neuron can hold more than its staked amount, all splits are rejected with `InsufficientFunds`.
7. Any call to `disburse_neuron` on a neuron with stake ≤ 200_000_000 finds `disburse_amount_e8s ≤ transaction_fee_e8s`, skips the decrement, and the ledger rejects the transfer — the neuron is permanently locked.
8. No new neurons can be staked because the minimum stake (100_000_000) is now ≤ the fee (200_000_000), violating the staking precondition.

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

**File:** rs/sns/governance/src/governance.rs (L1170-1172)
```rust
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
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

**File:** rs/sns/governance/src/types.rs (L620-624)
```rust
    /// Validates that the nervous system parameter transaction_fee_e8s is well-formed.
    fn validate_transaction_fee_e8s(&self) -> Result<u64, String> {
        self.transaction_fee_e8s
            .ok_or_else(|| "NervousSystemParameters.transaction_fee_e8s must be set".to_string())
    }
```
