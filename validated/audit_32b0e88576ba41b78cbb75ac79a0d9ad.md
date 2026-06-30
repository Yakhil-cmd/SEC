The vulnerability is real. Let me trace through the exact code path.

---

## Trace Analysis

**Preconditions**: `fixed_gas = Some(N)`, `block_base_fee_per_gas = 0` (Aurora default), EIP-1559 tx with `max_fee_per_gas = 0`, `max_priority_fee_per_gas = 0`.

### Step 1 — Early-return guard in `charge_gas` [1](#0-0) 

```rust
if transaction.max_fee_per_gas.is_zero()   // TRUE  (0 == 0)
    && fixed_gas.is_none()                  // FALSE (fixed_gas = Some(N))
    && block_base_fee_per_gas.is_zero()     // TRUE  (Aurora default)
{
    return Ok(GasPaymentResult::default()); // NOT TAKEN
}
```

The guard does **not** short-circuit because `fixed_gas.is_none()` is false. Execution continues.

### Step 2 — Base fee check passes [2](#0-1) 

`0 < 0` → false → no error.

### Step 3 — `effective_gas_price` computes to 0 [3](#0-2) 

```
priority_fee_per_gas = min(0, 0 - 0) = 0
effective_gas_price  = 0 + 0         = 0
```

### Step 4 — `prepaid_amount` computes to 0 [4](#0-3) 

```
prepaid_amount = fixed_gas.map_or(gas_limit, EthGas::as_u256) * effective_gas_price
               = N * 0
               = 0
```

`fixed_gas` controls only the *quantity* of gas charged, not the *price*. When `effective_gas_price = 0`, the product is always 0 regardless of `N`.

### Step 5 — No balance deduction [5](#0-4) 

`sender_balance - 0 = sender_balance` — the sender's balance is unchanged.

### Step 6 — `refund_unused_gas` exits immediately [6](#0-5) 

`effective_gas_price.is_zero()` → true → early return. No relayer reward is paid.

### Step 7 — No other guard in the submit path prevents this [7](#0-6) 

The only `fixed_gas`-related check is `fixed_gas > gas_limit` (overflow guard). The `max_priority_fee_per_gas > max_fee_per_gas` check passes because `0 > 0` is false. No check enforces `max_fee_per_gas > 0` when `fixed_gas` is set.

---

### Title
Free EVM Execution in Silo Mode via Zero `max_fee_per_gas` Bypassing `fixed_gas` Charge — (`engine/src/engine.rs`)

### Summary
When silo mode is active with `fixed_gas = Some(N)` and Aurora's default `block_base_fee_per_gas = 0`, an attacker can submit an EIP-1559 transaction with `max_fee_per_gas = 0`. The `charge_gas` function correctly skips the early-return guard (because `fixed_gas.is_none()` is false), but then computes `effective_gas_price = 0`, making `prepaid_amount = fixed_gas * 0 = 0`. The sender pays nothing, and the relayer receives no reward, while arbitrary EVM code executes.

### Finding Description
`charge_gas` derives `effective_gas_price` solely from `max_priority_fee_per_gas`, `max_fee_per_gas`, and `block_base_fee_per_gas`. [3](#0-2) 

`fixed_gas` is used only to substitute the *quantity* of gas in the `prepaid_amount` multiplication — it does not set a floor on the price. [4](#0-3) 

When all three price inputs are zero, `effective_gas_price = 0` and `prepaid_amount = 0`, regardless of `fixed_gas`. The early-return guard was intended to allow free transactions only when no fee mechanism is configured, but its `fixed_gas.is_none()` condition does not prevent the zero-price path from being reached when `fixed_gas` is set. [1](#0-0) 

### Impact Explanation
**Critical — Insolvency.** The silo operator configures `fixed_gas` to guarantee fee revenue per transaction. An attacker submits transactions with `max_fee_per_gas = 0`, executing arbitrary EVM code at zero cost. The relayer bears real NEAR gas costs but receives zero ETH fee reward. Repeated exploitation drains the protocol's fee revenue and enables unlimited free computation.

### Likelihood Explanation
Aurora's `block_base_fee_per_gas` is 0 by default (Aurora does not implement EIP-1559 base fee burning). Any whitelisted address in silo mode can craft a valid EIP-1559 transaction with `max_fee_per_gas = 0` — this is a standard, well-formed transaction. No special privilege or key compromise is required. The attack is trivially repeatable.

### Recommendation
In `charge_gas`, when `fixed_gas` is `Some(_)`, enforce that `effective_gas_price > 0` before proceeding, or derive a minimum required price from the transaction's `max_fee_per_gas` independently of `block_base_fee_per_gas`. Concretely, the early-return guard should only allow a zero-price path when `fixed_gas` is also `None`:

```rust
// Current (vulnerable):
if transaction.max_fee_per_gas.is_zero()
    && fixed_gas.is_none()
    && block_base_fee_per_gas.is_zero()
{
    return Ok(GasPaymentResult::default());
}

// After fix — also reject zero-price when fixed_gas is set:
if transaction.max_fee_per_gas.is_zero()
    && fixed_gas.is_none()
    && block_base_fee_per_gas.is_zero()
{
    return Ok(GasPaymentResult::default());
}
if fixed_gas.is_some() && effective_gas_price.is_zero() {
    return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee); // or a new error variant
}
```

Alternatively, require `max_fee_per_gas > 0` as a precondition when `fixed_gas` is set.

### Proof of Concept

```rust
#[test]
fn test_fixed_gas_zero_price_bypass() {
    let origin = Address::zero();
    let current_account_id = AccountId::default();
    let env = Fixed::default(); // block_base_fee_per_gas = 0
    let storage = RefCell::new(Storage::default());
    let mut io = StoragePointer(&storage);
    let initial_balance = Wei::new_u64(1_000_000);
    add_balance(&mut io, &origin, initial_balance).unwrap();

    let mut engine: Engine<_, _> =
        Engine::new_with_state(EngineState::default(), origin, current_account_id, io, &env);

    let transaction = NormalizedEthTransaction {
        address: Address::default(),
        chain_id: None,
        nonce: U256::default(),
        gas_limit: 100_000.into(),
        max_priority_fee_per_gas: U256::zero(), // attacker sets 0
        max_fee_per_gas: U256::zero(),           // attacker sets 0
        to: Some(Address::default()),
        value: Wei::default(),
        data: vec![],
        access_list: vec![],
        authorization_list: vec![],
    };

    let fixed_gas = Some(EthGas::new(1_000)); // silo operator sets fixed gas

    let result = engine
        .charge_gas(&origin, &transaction, None, fixed_gas)
        .unwrap();

    // BUG: prepaid_amount is 0 even though fixed_gas is set
    assert_eq!(result.prepaid_amount, Wei::zero());
    assert_eq!(result.effective_gas_price, U256::zero());

    // Sender balance is unchanged — they paid nothing
    let balance_after = get_balance(&engine.io, &origin);
    assert_eq!(balance_after, initial_balance); // passes, proving free execution
}
``` [8](#0-7) [9](#0-8)

### Citations

**File:** engine/src/engine.rs (L468-515)
```rust
    pub fn charge_gas(
        &mut self,
        sender: &Address,
        transaction: &NormalizedEthTransaction,
        max_gas_price: Option<U256>,
        fixed_gas: Option<EthGas>,
    ) -> Result<GasPaymentResult, GasPaymentError> {
        let block_base_fee_per_gas = self.block_base_fee_per_gas();
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

        let new_balance = get_balance(&self.io, sender)
            .checked_sub(prepaid_amount)
            .ok_or(GasPaymentError::OutOfFund)?;

        set_balance(&mut self.io, sender, &new_balance);

        self.gas_price = effective_gas_price;

        Ok(GasPaymentResult {
            prepaid_amount,
            effective_gas_price,
            priority_fee_per_gas,
        })
    }
```

**File:** engine/src/engine.rs (L1066-1085)
```rust
    if fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit) {
        return Err(EngineErrorKind::FixedGasOverflow.into());
    }

    let intrinsic_gas = transaction
        .intrinsic_gas(CONFIG)
        .map_err(|_| EngineErrorKind::GasOverflow)?;
    let floor_gas = transaction
        .floor_gas(CONFIG)
        .map_err(|_| EngineErrorKind::GasOverflow)?;

    // Check that the max value of intrinsic gas and floor gas is covered by the transaction
    // gas limit, EIP-7623 https://eips.ethereum.org/EIPS/eip-7623
    if transaction.gas_limit < core::cmp::max(intrinsic_gas, floor_gas).into() {
        return Err(EngineErrorKind::IntrinsicGasNotMet.into());
    }

    if transaction.max_priority_fee_per_gas > transaction.max_fee_per_gas {
        return Err(EngineErrorKind::MaxPriorityGasFeeTooLarge.into());
    }
```

**File:** engine/src/engine.rs (L1262-1272)
```rust
pub fn refund_unused_gas<I: IO>(
    io: &mut I,
    sender: &Address,
    gas_used: u64,
    gas_result: &GasPaymentResult,
    relayer: &Address,
    fixed_gas: Option<EthGas>,
) -> Result<(), GasPaymentError> {
    if gas_result.effective_gas_price.is_zero() {
        return Ok(());
    }
```
