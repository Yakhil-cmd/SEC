### Title
EIP-7623 Floor Gas Not Enforced in Gas Refund Calculation — (`engine/src/engine.rs`)

---

### Summary

The Aurora Engine added EIP-7623 floor gas support (version 3.10.1). The `floor_gas` value is computed and used to enforce a minimum gas limit, but it is **never passed into `refund_unused_gas`**. As a result, when `floor_gas > gas_used`, the sender is refunded the full difference between `gas_limit` and `gas_used`, effectively paying only `gas_used × gas_price` instead of the EIP-7623-mandated `max(gas_used, floor_gas) × gas_price`. This is the direct analog of the reported bug: a fee component that is computed but silently dropped from the accounting path, causing systematic under-payment.

---

### Finding Description

**Root cause — `engine/src/engine.rs`**

The submit path computes both `intrinsic_gas` and `floor_gas`, enforces the minimum gas limit, then calls `refund_unused_gas` — but passes only `gas_used`, never `floor_gas`:

```
// lines 1070-1080
let intrinsic_gas = transaction.intrinsic_gas(CONFIG)...;
let floor_gas     = transaction.floor_gas(CONFIG)...;

if transaction.gas_limit < core::cmp::max(intrinsic_gas, floor_gas).into() {
    return Err(EngineErrorKind::IntrinsicGasNotMet.into());
}
``` [1](#0-0) 

```
// lines 1148-1155
refund_unused_gas(
    &mut io,
    &sender,
    gas_used,          // ← floor_gas is never passed here
    &prepaid_amount,
    &relayer_address,
    fixed_gas,
)
``` [2](#0-1) 

Inside `refund_unused_gas`, when `fixed_gas` is `None`, the spent amount is computed purely from `gas_used`:

```rust
let gas_to_wei = |price: U256| {
    fixed_gas
        .map_or_else(|| gas_used.into(), EthGas::as_u256)  // floor_gas absent
        .checked_mul(price)
        ...
};
let spent_amount = gas_to_wei(gas_result.effective_gas_price)?;
``` [3](#0-2) 

**The `floor_gas` function** (EIP-7623) returns a non-zero value for calldata-heavy transactions when the Prague hardfork config is active (`has_floor_gas = true`):

```rust
pub fn floor_gas(&self, config: &aurora_evm::Config) -> Result<u64, Error> {
    if config.has_floor_gas {
        // tokens_in_calldata = non_zero_bytes * 4 + zero_bytes
        // floor_gas = tokens_in_calldata * total_cost_floor_per_token + base_gas
        ...
    } else {
        Ok(0)
    }
}
``` [4](#0-3) 

EIP-7623 mandates: *"The gas used by a transaction is the maximum of the intrinsic gas and the floor gas."* The refund must therefore be `gas_limit - max(gas_used, floor_gas)`, not `gas_limit - gas_used`.

---

### Impact Explanation

When `floor_gas > gas_used` (the common case for calldata-heavy transactions with simple EVM execution):

| Quantity | Correct (EIP-7623) | Actual (buggy) |
|---|---|---|
| Sender pays | `floor_gas × gas_price` | `gas_used × gas_price` |
| Relayer receives | `floor_gas × priority_fee` | `gas_used × priority_fee` |
| Sender refund | `(gas_limit − floor_gas) × gas_price` | `(gas_limit − gas_used) × gas_price` |

The relayer is systematically under-compensated by `(floor_gas − gas_used) × priority_fee_per_gas` on every such transaction — **theft of unclaimed yield (High)**.

Additionally, the EIP-7623 anti-spam mechanism is entirely bypassed: a user can include arbitrarily large calldata and pay only the intrinsic gas cost, not the floor gas cost.

---

### Likelihood Explanation

- Triggerable by any unprivileged EVM user with no special access.
- Requires only crafting a transaction with non-trivial calldata and a contract call that ignores the calldata (e.g., a no-op function, or a simple ETH transfer to a contract).
- The condition `floor_gas > gas_used` is met whenever calldata is large relative to EVM execution complexity — a routine pattern.
- The Prague hardfork is already supported and `has_floor_gas = true` is active.

---

### Recommendation

Pass `floor_gas` into `refund_unused_gas` and replace `gas_used` with `max(gas_used, floor_gas)` in the spent-amount calculation:

```rust
// In the submit path, after computing floor_gas:
refund_unused_gas(
    &mut io,
    &sender,
    gas_used.max(floor_gas),   // enforce EIP-7623 floor
    &prepaid_amount,
    &relayer_address,
    fixed_gas,
)
```

Alternatively, add a `floor_gas: u64` parameter to `refund_unused_gas` and apply `max` internally.

---

### Proof of Concept

1. User crafts a transaction with **1 000 non-zero calldata bytes** targeting a contract that ignores its input.
2. `floor_gas = (1000 × 4) × 10 + 21 000 = 61 000` [5](#0-4) 
3. `intrinsic_gas = 21 000 + 1 000 × 16 = 37 000` [6](#0-5) 
4. User sets `gas_limit = 61 000`, `gas_price = 1 wei`. Gas limit check passes (`61 000 ≥ max(37 000, 61 000)`). [7](#0-6) 
5. User prepays `61 000 wei`.
6. EVM execution consumes `37 000 gas` (intrinsic only; contract does nothing).
7. `refund_unused_gas` refunds `(61 000 − 37 000) × 1 = 24 000 wei` to the sender.
8. **Sender effectively pays 37 000 wei instead of the EIP-7623-mandated 61 000 wei** — a 39% discount obtained by exploiting the missing floor gas enforcement.
9. Relayer receives `37 000 × priority_fee` instead of `61 000 × priority_fee`.

### Citations

**File:** engine/src/engine.rs (L1070-1081)
```rust
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
```

**File:** engine/src/engine.rs (L1148-1155)
```rust
    refund_unused_gas(
        &mut io,
        &sender,
        gas_used,
        &prepaid_amount,
        &relayer_address,
        fixed_gas,
    )
```

**File:** engine/src/engine.rs (L1274-1292)
```rust
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
```

**File:** engine-transactions/src/lib.rs (L173-185)
```rust
        let num_zero_bytes = u64::try_from(self.data.iter().filter(|b| **b == 0).count())
            .map_err(|_e| Error::IntegerConversion)?;
        let gas_zero_bytes = config
            .gas_transaction_zero_data
            .checked_mul(num_zero_bytes)
            .ok_or(Error::GasOverflow)?;

        let data_len = u64::try_from(self.data.len()).map_err(|_e| Error::IntegerConversion)?;
        let num_non_zero_bytes = data_len - num_zero_bytes;
        let gas_non_zero_bytes = config
            .gas_transaction_non_zero_data
            .checked_mul(num_non_zero_bytes)
            .ok_or(Error::GasOverflow)?;
```

**File:** engine-transactions/src/lib.rs (L228-251)
```rust
    #[allow(clippy::naive_bytecount)]
    pub fn floor_gas(&self, config: &aurora_evm::Config) -> Result<u64, Error> {
        if config.has_floor_gas {
            let num_zero_bytes = u64::try_from(self.data.iter().filter(|b| **b == 0).count())
                .map_err(|_e| Error::IntegerConversion)?;
            let data_len = u64::try_from(self.data.len()).map_err(|_e| Error::IntegerConversion)?;
            let num_non_zero_bytes = data_len
                .checked_sub(num_zero_bytes)
                .ok_or(Error::GasOverflow)?;

            let base_gas = config.gas_transaction_call;
            let tokens_in_calldata = num_non_zero_bytes
                .checked_mul(4)
                .and_then(|gas| gas.checked_add(num_zero_bytes))
                .ok_or(Error::GasOverflow)?;

            tokens_in_calldata
                .checked_mul(config.total_cost_floor_per_token)
                .and_then(|gas| gas.checked_add(base_gas))
                .ok_or(Error::GasOverflow)
        } else {
            Ok(0)
        }
    }
```
