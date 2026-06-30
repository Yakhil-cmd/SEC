### Title
Free Transaction Execution Bypass in Silo Mode via Zero `max_fee_per_gas` — (File: `engine/src/engine.rs`)

### Summary

In Silo mode with `fixed_gas` configured, any EVM user can bypass the fixed gas fee mechanism by submitting a transaction with `max_fee_per_gas = 0`. The `charge_gas()` function correctly skips the zero-fee early return when `fixed_gas` is set, but then computes `effective_gas_price = 0` because `block_base_fee_per_gas` is hardcoded to zero. The result is `prepaid_amount = fixed_gas * 0 = 0`: the user pays nothing, and relayers receive no reward.

### Finding Description

`charge_gas()` in `engine/src/engine.rs` contains the following early-return guard:

```rust
if transaction.max_fee_per_gas.is_zero()
    && fixed_gas.is_none()
    && block_base_fee_per_gas.is_zero()
{
    return Ok(GasPaymentResult::default());
}
``` [1](#0-0) 

When Silo mode is active (`fixed_gas = Some(…)`), `fixed_gas.is_none()` is `false`, so the early return is correctly skipped. However, `block_base_fee_per_gas` is hardcoded to always return `U256::zero()`:

```rust
fn block_base_fee_per_gas(&self) -> U256 {
    U256::zero()
}
``` [2](#0-1) 

Because of this, the subsequent computation:

```rust
let priority_fee_per_gas = transaction
    .max_priority_fee_per_gas
    .min(transaction.max_fee_per_gas - block_base_fee_per_gas);
let effective_gas_price = priority_fee_per_gas + block_base_fee_per_gas;
``` [3](#0-2) 

reduces to `effective_gas_price = min(max_priority_fee_per_gas, max_fee_per_gas)`. If a user sets `max_fee_per_gas = 0`, then `effective_gas_price = 0`, and:

```rust
let prepaid_amount = fixed_gas
    .map_or(transaction.gas_limit, EthGas::as_u256)
    .checked_mul(effective_gas_price)
    .map(Wei::new)
    .ok_or(GasPaymentError::EthAmountOverflow)?;
``` [4](#0-3) 

yields `prepaid_amount = fixed_gas * 0 = 0`. No ETH is deducted from the sender. The `refund_unused_gas` function also short-circuits on `effective_gas_price.is_zero()`, so the relayer receives nothing either. [5](#0-4) 

The only Silo-mode validation in `submit_with_alt_modexp` checks that `fixed_gas <= gas_limit`, but does not validate that `max_fee_per_gas > 0`:

```rust
if fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit) {
    return Err(EngineErrorKind::FixedGasOverflow.into());
}
``` [6](#0-5) 

### Impact Explanation

**High — Theft of unclaimed yield.** Silo mode is designed for private/enterprise EVM instances where operators configure `fixed_gas` to guarantee predictable fee revenue for relayers. Any user who sets `max_fee_per_gas = 0` executes transactions for free, depriving relayers of their expected reward. Because `block_base_fee_per_gas` is permanently zero and cannot be configured, there is no existing mechanism to enforce a minimum price in Silo mode.

### Likelihood Explanation

**High.** The bypass requires only that the attacker set `max_fee_per_gas = 0` in a standard EVM transaction submitted via the public `submit()` or `submit_with_args()` entrypoints. No special role, privilege, or key is needed. Any EVM user interacting with a Silo instance can exploit this immediately.

### Recommendation

In `charge_gas()`, when `fixed_gas` is `Some`, validate that `effective_gas_price > 0` before computing `prepaid_amount`, or add an explicit check that `max_fee_per_gas > 0` when Silo mode is active:

```rust
if fixed_gas.is_some() && effective_gas_price.is_zero() {
    return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee);
}
```

Alternatively, allow Silo operators to configure a non-zero `block_base_fee_per_gas` so that the existing `max_fee_per_gas < block_base_fee_per_gas` guard can enforce a minimum price.

### Proof of Concept

1. Silo operator calls `set_silo_params` with `fixed_gas = EthGas::new(1_000_000)`.
2. Attacker submits a transaction via `submit()` with `max_fee_per_gas = 0` and `max_priority_fee_per_gas = 0`, and `gas_limit >= 1_000_000`.
3. In `charge_gas()`:
   - Early return NOT triggered (`fixed_gas.is_some()`).
   - `effective_gas_price = min(0, 0) + 0 = 0`.
   - `prepaid_amount = 1_000_000 * 0 = 0`.
   - Sender balance unchanged; relayer reward = 0.
4. Transaction executes successfully at zero cost. The Silo operator's fee mechanism is completely bypassed.

### Citations

**File:** engine/src/engine.rs (L476-481)
```rust
        if transaction.max_fee_per_gas.is_zero()
            && fixed_gas.is_none()
            && block_base_fee_per_gas.is_zero()
        {
            return Ok(GasPaymentResult::default());
        }
```

**File:** engine/src/engine.rs (L487-493)
```rust
        let priority_fee_per_gas = transaction
            .max_priority_fee_per_gas
            .min(transaction.max_fee_per_gas - block_base_fee_per_gas);
        let priority_fee_per_gas = max_gas_price.map_or(priority_fee_per_gas, |price| {
            price.min(priority_fee_per_gas)
        });
        let effective_gas_price = priority_fee_per_gas + block_base_fee_per_gas;
```

**File:** engine/src/engine.rs (L496-500)
```rust
        let prepaid_amount = fixed_gas
            .map_or(transaction.gas_limit, EthGas::as_u256)
            .checked_mul(effective_gas_price)
            .map(Wei::new)
            .ok_or(GasPaymentError::EthAmountOverflow)?;
```

**File:** engine/src/engine.rs (L1066-1068)
```rust
    if fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit) {
        return Err(EngineErrorKind::FixedGasOverflow.into());
    }
```

**File:** engine/src/engine.rs (L1270-1272)
```rust
    if gas_result.effective_gas_price.is_zero() {
        return Ok(());
    }
```

**File:** engine/src/engine.rs (L1869-1871)
```rust
    fn block_base_fee_per_gas(&self) -> U256 {
        U256::zero()
    }
```
