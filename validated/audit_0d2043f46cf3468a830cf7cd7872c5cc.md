### Title
Fixed Gas Silo Mode Allows Execution of Arbitrarily Expensive Transactions at a Flat Fee, Enabling Cheap Block Stuffing - (`engine/src/engine.rs`)

---

### Summary

In Aurora Engine's Silo mode, when `fixed_gas` is configured, the fee charged to a sender is computed using the `fixed_gas` quantity rather than the transaction's `gas_limit`. However, the EVM execution itself is bounded by the full `gas_limit` from the transaction. Because the only validation is that `fixed_gas <= gas_limit`, an attacker can set `gas_limit` to an arbitrarily large value (up to `u64::MAX`), pay only `fixed_gas * gas_price`, and execute EVM computation many orders of magnitude more expensive than what they paid for. This is a direct analog to the Aleo `split` transaction fixed-fee issue: a hardcoded/flat fee that decouples payment from actual resource consumption, enabling cheap block-stuffing attacks.

---

### Finding Description

The `submit_with_alt_modexp` function in `engine/src/engine.rs` retrieves the Silo `fixed_gas` value and passes it to `charge_gas`, which uses it as the gas quantity for fee deduction: [1](#0-0) 

```rust
let fixed_gas = silo::get_fixed_gas(&io);
```

Inside `charge_gas`, when `fixed_gas` is `Some`, the prepaid amount is `fixed_gas * effective_gas_price` — not `gas_limit * effective_gas_price`: [2](#0-1) 

```rust
let prepaid_amount = fixed_gas
    .map_or(transaction.gas_limit, EthGas::as_u256)
    .checked_mul(effective_gas_price)
    .map(Wei::new)
    .ok_or(GasPaymentError::EthAmountOverflow)?;
```

The only guard against `gas_limit >> fixed_gas` is: [3](#0-2) 

```rust
if fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit) {
    return Err(EngineErrorKind::FixedGasOverflow.into());
}
```

This rejects only the case where `fixed_gas > gas_limit`. It explicitly **allows** `gas_limit >> fixed_gas`.

The EVM execution then uses the full `gas_limit`: [4](#0-3) 

```rust
let gas_limit = transaction.gas_limit.try_into()...;
let result = if let Some(receiver) = transaction.to {
    engine.call(&sender, &receiver, ..., gas_limit, ...)
} else {
    engine.deploy_code(sender, ..., gas_limit, ...)
};
```

Similarly, `refund_unused_gas` uses `fixed_gas` (not `gas_used`) to compute the spent amount, so the user is charged exactly `fixed_gas * gas_price` regardless of actual EVM gas consumed: [5](#0-4) 

```rust
let gas_to_wei = |price: U256| {
    fixed_gas
        .map_or_else(|| gas_used.into(), EthGas::as_u256)
        .checked_mul(price)
        ...
};
let spent_amount = gas_to_wei(gas_result.effective_gas_price)?;
```

The `fixed_gas` value is stored and retrieved from Silo storage: [6](#0-5) 

The `SiloParamsArgs` struct confirms `fixed_gas` is a flat gas quantity, not a dynamic limit: [7](#0-6) 

---

### Impact Explanation

An attacker in a Silo deployment (with whitelists disabled, which is the default) can submit transactions with `gas_limit = u64::MAX` while paying only `fixed_gas * gas_price`. The EVM executes up to `gas_limit` gas of computation. This allows:

- Submitting computationally expensive transactions (e.g., large loops, heavy contract calls) at a flat, predictable, low cost.
- Filling NEAR blocks with Aurora transactions that consume far more NEAR gas than the EVM fee accounts for.
- Crowding out legitimate transactions, causing **temporary freezing of funds** for users of the Silo deployment — DeFi protocols relying on timely liquidations, collateral adjustments, or withdrawals are directly affected.

This matches the **High — Temporary freezing of funds** impact category.

---

### Likelihood Explanation

- Silo mode is a production feature of Aurora Engine, not a test-only path.
- Whitelists are **disabled by default** (`!list.is_enabled()` returns `true` when not active), meaning any EVM user can submit transactions in a Silo deployment that has not explicitly enabled whitelists.
- The attack requires only a valid EVM transaction with a high `gas_limit` — no special privileges, no leaked keys, no governance capture.
- The `fixed_gas` value is set by the operator and is publicly readable from storage, so an attacker can trivially compute the exact fee they will pay and craft maximally expensive transactions. [8](#0-7) 

---

### Recommendation

In `submit_with_alt_modexp`, enforce that `transaction.gas_limit` cannot exceed `fixed_gas` (or a small multiple of it) when Silo fixed-gas mode is active. Alternatively, use `gas_used` (actual EVM gas consumed) as the basis for the fee when `fixed_gas` is set, so that the flat fee represents a minimum rather than a ceiling. At minimum, add a validation that rejects transactions where `gas_limit > fixed_gas` in Silo mode, mirroring the intent of the `FixedGasOverflow` check in the opposite direction.

---

### Proof of Concept

1. Deploy Aurora Engine in Silo mode. Set `fixed_gas = 1_000_000` via `set_silo_params`. Leave whitelists disabled (default).
2. Craft an EVM transaction with:
   - `gas_limit = 10_000_000_000` (10 billion, well above `fixed_gas`)
   - `gas_price = 1`
   - `to` = address of a computationally expensive contract (e.g., a tight loop consuming all available gas)
3. Submit via the `submit` entrypoint.
4. Observe: the sender is charged `1_000_000 * 1 = 1_000_000 wei`, but the EVM executes up to `10_000_000_000` gas of computation.
5. Repeat with many accounts to fill NEAR blocks, preventing legitimate transactions from being included.

The fee paid is `fixed_gas * gas_price` regardless of actual execution cost, confirmed by `charge_gas` at: [9](#0-8) 

and `refund_unused_gas` at: [5](#0-4)

### Citations

**File:** engine/src/engine.rs (L494-500)
```rust
        // First, we try to use `fixed_gas`. At this point we already know that the `fixed_gas` is
        // less than the `gas_limit`. It allows avoiding refunding unused gas to the sender later.
        let prepaid_amount = fixed_gas
            .map_or(transaction.gas_limit, EthGas::as_u256)
            .checked_mul(effective_gas_price)
            .map(Wei::new)
            .ok_or(GasPaymentError::EthAmountOverflow)?;
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

**File:** engine/src/engine.rs (L1107-1127)
```rust
    let gas_limit = transaction
        .gas_limit
        .try_into()
        .map_err(|_| EngineErrorKind::GasOverflow)?;
    let access_list = transaction
        .access_list
        .into_iter()
        .map(|a| (a.address, a.storage_keys))
        .collect();
    let result = if let Some(receiver) = transaction.to {
        engine.call(
            &sender,
            &receiver,
            transaction.value,
            transaction.data,
            gas_limit,
            access_list,
            transaction.authorization_list,
            handler,
        )
        // TODO: charge for storage
```

**File:** engine/src/engine.rs (L1275-1284)
```rust
        let gas_to_wei = |price: U256| {
            fixed_gas
                .map_or_else(|| gas_used.into(), EthGas::as_u256)
                .checked_mul(price)
                .map(Wei::new)
                .ok_or(GasPaymentError::EthAmountOverflow)
        };

        let spent_amount = gas_to_wei(gas_result.effective_gas_price)?;
        let reward_amount = gas_to_wei(gas_result.priority_fee_per_gas)?;
```

**File:** engine/src/contract_methods/silo/mod.rs (L41-45)
```rust
pub fn get_fixed_gas<I: IO>(io: &I) -> Option<EthGas> {
    let key = fixed_gas_key();
    io.read_storage(&key)
        .and_then(|bytes| bytes.to_value().ok())
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L160-163)
```rust
fn is_account_allowed<I: IO + Copy>(io: &I, account: &AccountId) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Account);
    !list.is_enabled() || list.is_exist(account)
}
```

**File:** engine-types/src/parameters/silo.rs (L15-24)
```rust
#[derive(Debug, Default, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
pub struct SiloParamsArgs {
    /// Fixed amount of gas per transaction.
    pub fixed_gas: EthGas,
    /// EVM address, which is used for withdrawing ERC-20 base tokens in case
    /// a recipient of the tokens is not in the silo white list.
    /// Note: the logic described above works only if the fallback address
    /// is set by `set_silo_params` function. In other words, in Silo mode.
    pub erc20_fallback_address: Address,
}
```
