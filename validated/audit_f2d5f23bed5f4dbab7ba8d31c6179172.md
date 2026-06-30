The vulnerability is real and concrete. Here is the full trace:

---

### Title
Zero-price free execution in silo mode bypasses `fixed_gas` fee enforcement — (`engine/src/engine.rs`)

### Summary
When silo mode is active (`fixed_gas.is_some()`), an attacker can submit a transaction with `max_fee_per_gas = 0` and `max_priority_fee_per_gas = 0`. Because Aurora's `block_base_fee_per_gas` is also 0, `effective_gas_price` resolves to 0, making `prepaid_amount = fixed_gas * 0 = 0`. The transaction executes for free and the relayer receives nothing.

### Finding Description

The early-return guard in `charge_gas` is:

```rust
if transaction.max_fee_per_gas.is_zero()
    && fixed_gas.is_none()
    && block_base_fee_per_gas.is_zero()
{
    return Ok(GasPaymentResult::default());
}
``` [1](#0-0) 

The intent is to short-circuit when there is genuinely no gas cost. However, the condition requires `fixed_gas.is_none()`. When `fixed_gas.is_some()` (silo mode), the guard is skipped even if `max_fee_per_gas = 0`. Execution falls through to:

```rust
let priority_fee_per_gas = transaction
    .max_priority_fee_per_gas
    .min(transaction.max_fee_per_gas - block_base_fee_per_gas);
// = min(0, 0 - 0) = 0
let effective_gas_price = priority_fee_per_gas + block_base_fee_per_gas;
// = 0 + 0 = 0
let prepaid_amount = fixed_gas
    .map_or(transaction.gas_limit, EthGas::as_u256)
    .checked_mul(effective_gas_price)   // fixed_gas * 0 = 0
    .map(Wei::new)
    .ok_or(GasPaymentError::EthAmountOverflow)?;
``` [2](#0-1) 

`prepaid_amount = 0`, so `new_balance = sender_balance - 0` — no ETH is deducted from the sender. [3](#0-2) 

In `refund_unused_gas`, the relayer reward path is also skipped:

```rust
if gas_result.effective_gas_price.is_zero() {
    return Ok(());
}
``` [4](#0-3) 

There is no check anywhere in the submission path (`submit_with_args`) that enforces `max_fee_per_gas > 0` when `fixed_gas` is set. The only pre-`charge_gas` validation relevant to gas price is:

```rust
if transaction.max_priority_fee_per_gas > transaction.max_fee_per_gas {
    return Err(EngineErrorKind::MaxPriorityGasFeeTooLarge.into());
}
``` [5](#0-4) 

`0 > 0` is false, so this does not block the attack.

### Impact Explanation

**Critical — Insolvency.** In silo mode the operator sets `fixed_gas` precisely to guarantee that every accepted transaction pays a deterministic fee to the relayer. With this bug, any whitelisted sender (or any sender when whitelists are disabled) can submit unlimited transactions at zero cost. The relayer's expected revenue (`fixed_gas * gas_price`) never materialises. Repeated free transactions create a persistent, unbounded gap between expected fee revenue and actual ETH collected, constituting protocol insolvency.

### Likelihood Explanation

Aurora's `block_base_fee_per_gas` is 0 by default. An EIP-1559 transaction with `max_fee_per_gas = 0` and `max_priority_fee_per_gas = 0` is a valid, well-formed transaction (the only constraint is `max_priority_fee_per_gas ≤ max_fee_per_gas`, which holds for `0 ≤ 0`). No special privilege is required beyond being a whitelisted submitter (or operating when whitelists are disabled). The attack is trivially constructable by any EVM user.

### Recommendation

Add an explicit guard in `charge_gas` (or in `submit_with_args` before calling it) that rejects a transaction when `fixed_gas.is_some()` and `effective_gas_price` would be zero:

```rust
// After computing effective_gas_price:
if fixed_gas.is_some() && effective_gas_price.is_zero() {
    return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee); // or a new dedicated error
}
```

Alternatively, restructure the early-return condition to also cover the silo case:

```rust
if transaction.max_fee_per_gas.is_zero()
    && block_base_fee_per_gas.is_zero()
    && fixed_gas.is_none()   // existing: free-tx path
{
    return Ok(GasPaymentResult::default());
}
// NEW: reject zero-price in silo mode
if transaction.max_fee_per_gas.is_zero()
    && block_base_fee_per_gas.is_zero()
    && fixed_gas.is_some()
{
    return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee);
}
```

### Proof of Concept

Call sequence on unmodified code:

1. Owner calls `set_silo_params` with `fixed_gas = 1_000_000`. [6](#0-5) 

2. Attacker (whitelisted address) submits an EIP-1559 transaction with:
   - `max_fee_per_gas = 0`
   - `max_priority_fee_per_gas = 0`
   - `gas_limit ≥ fixed_gas` (e.g. `2_000_000`)

3. In `submit_with_args`:
   - `fixed_gas = Some(1_000_000)` is read from storage. [7](#0-6) 
   - `FixedGasOverflow` check passes (`1_000_000 ≤ 2_000_000`). [8](#0-7) 
   - `charge_gas` is called with `fixed_gas = Some(1_000_000)`. [9](#0-8) 

4. Inside `charge_gas`:
   - Early-return condition: `0.is_zero() && false && 0.is_zero()` → **false**, not taken.
   - `effective_gas_price = 0`.
   - `prepaid_amount = 1_000_000 * 0 = 0`.
   - Sender balance unchanged. [10](#0-9) 

5. Transaction executes successfully.

6. `refund_unused_gas` early-returns on `effective_gas_price.is_zero()` → relayer balance unchanged. [4](#0-3) 

**Assertion:** relayer balance remains 0 after execution; sender paid 0 gas; `fixed_gas * 1_000_000` ETH of expected revenue was never collected.

### Citations

**File:** engine/src/engine.rs (L476-500)
```rust
        if transaction.max_fee_per_gas.is_zero()
            && fixed_gas.is_none()
            && block_base_fee_per_gas.is_zero()
        {
            return Ok(GasPaymentResult::default());
        }

        if transaction.max_fee_per_gas < block_base_fee_per_gas {
            return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee);
        }

        let priority_fee_per_gas = transaction
            .max_priority_fee_per_gas
            .min(transaction.max_fee_per_gas - block_base_fee_per_gas);
        let priority_fee_per_gas = max_gas_price.map_or(priority_fee_per_gas, |price| {
            price.min(priority_fee_per_gas)
        });
        let effective_gas_price = priority_fee_per_gas + block_base_fee_per_gas;
        // First, we try to use `fixed_gas`. At this point we already know that the `fixed_gas` is
        // less than the `gas_limit`. It allows avoiding refunding unused gas to the sender later.
        let prepaid_amount = fixed_gas
            .map_or(transaction.gas_limit, EthGas::as_u256)
            .checked_mul(effective_gas_price)
            .map(Wei::new)
            .ok_or(GasPaymentError::EthAmountOverflow)?;
```

**File:** engine/src/engine.rs (L502-506)
```rust
        let new_balance = get_balance(&self.io, sender)
            .checked_sub(prepaid_amount)
            .ok_or(GasPaymentError::OutOfFund)?;

        set_balance(&mut self.io, sender, &new_balance);
```

**File:** engine/src/engine.rs (L1049-1049)
```rust
    let fixed_gas = silo::get_fixed_gas(&io);
```

**File:** engine/src/engine.rs (L1066-1068)
```rust
    if fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit) {
        return Err(EngineErrorKind::FixedGasOverflow.into());
    }
```

**File:** engine/src/engine.rs (L1083-1085)
```rust
    if transaction.max_priority_fee_per_gas > transaction.max_fee_per_gas {
        return Err(EngineErrorKind::MaxPriorityGasFeeTooLarge.into());
    }
```

**File:** engine/src/engine.rs (L1101-1106)
```rust
    let prepaid_amount = match engine.charge_gas(&sender, &transaction, max_gas_price, fixed_gas) {
        Ok(gas_result) => gas_result,
        Err(err) => {
            return Err(EngineErrorKind::GasPayment(err).into());
        }
    };
```

**File:** engine/src/engine.rs (L1270-1272)
```rust
    if gas_result.effective_gas_price.is_zero() {
        return Ok(());
    }
```

**File:** engine/src/lib.rs (L829-838)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_silo_params() {
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: Option<SiloParamsArgs> = io.read_input_borsh().sdk_unwrap();
        silo::set_silo_params(&mut io, args);
```
