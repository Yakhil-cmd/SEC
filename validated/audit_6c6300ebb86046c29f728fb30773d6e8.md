### Title
Silo Owner Can Frontrun User Transactions by Raising `fixed_gas` Without Timelock, Stealing ETH - (`engine/src/engine.rs`)

---

### Summary

In Aurora Engine's Silo mode, the `fixed_gas` parameter is read directly from storage at the moment a user's `submit`/`submit_with_args` transaction executes. The Silo owner can call `set_fixed_gas` (or `set_silo_params`) at any time with no timelock, and users have no mechanism to specify a maximum `fixed_gas` they are willing to accept. A malicious Silo owner can frontrun a pending user transaction by raising `fixed_gas` to just below the user's `gas_limit`, causing the user to pay far more ETH than expected. The excess ETH flows to the relayer address, which the Silo owner controls.

---

### Finding Description

During transaction execution, `fixed_gas` is fetched live from NEAR storage:

```rust
// engine/src/engine.rs:1049
let fixed_gas = silo::get_fixed_gas(&io);
```

This value is then passed directly into `charge_gas`, which computes the full prepaid amount as:

```rust
// engine/src/engine.rs:496-500
let prepaid_amount = fixed_gas
    .map_or(transaction.gas_limit, EthGas::as_u256)
    .checked_mul(effective_gas_price)
    .map(Wei::new)
    .ok_or(GasPaymentError::EthAmountOverflow)?;
```

The only guard on `fixed_gas` is that it must not exceed the user's own `gas_limit`:

```rust
// engine/src/engine.rs:1066-1068
if fixed_gas.is_some_and(|gas| gas.as_u256() > transaction.gas_limit) {
    return Err(EngineErrorKind::FixedGasOverflow.into());
}
```

This means the owner can set `fixed_gas` to any value up to the user's `gas_limit`. Because `refund_unused_gas` also uses `fixed_gas` (not actual `gas_used`) to compute `spent_amount`, the user receives zero refund:

```rust
// engine/src/engine.rs:1276-1283
let gas_to_wei = |price: U256| {
    fixed_gas
        .map_or_else(|| gas_used.into(), EthGas::as_u256)
        .checked_mul(price)
        ...
};
let spent_amount = gas_to_wei(gas_result.effective_gas_price)?;
// prepaid_amount == spent_amount => refund == 0
```

The relayer then receives `fixed_gas * priority_fee_per_gas` as a reward. The Silo owner controls the relayer.

The `set_fixed_gas` entry point has no timelock:

```rust
// engine/src/lib.rs:784-792
pub extern "C" fn set_fixed_gas() {
    let mut io = Runtime;
    let state = state::get_state(&io).sdk_unwrap();
    require_owner_and_running(&state, &io.predecessor_account_id())
        ...
    silo::set_fixed_gas(&mut io, args.fixed_gas);
}
```

The `SubmitArgs` struct exposes `max_gas_price` to let users cap the gas *price*, but provides no equivalent `max_fixed_gas` field to cap the gas *amount*:

```rust
// engine-types/src/parameters/engine.rs:132-140
pub struct SubmitArgs {
    pub tx_data: Vec<u8>,
    pub max_gas_price: Option<u128>,   // caps price only
    pub gas_token_address: Option<Address>,
    // no max_fixed_gas field
}
```

---

### Impact Explanation

A malicious Silo owner can steal ETH directly from any user transacting in the Silo. By raising `fixed_gas` to the user's full `gas_limit` just before the user's transaction executes, the owner extracts `gas_limit * effective_gas_price` ETH from the user's Aurora balance instead of the small amount the user expected to pay. The stolen ETH is credited to the relayer address, which the Silo owner controls. This is a direct, at-rest theft of user funds — **Critical** impact.

---

### Likelihood Explanation

The Silo owner is a single privileged NEAR account. On NEAR, transaction ordering within a block is deterministic and observable. The owner can submit `set_fixed_gas` in the same block as a user's `submit` call, ordering it first. No external dependency or key compromise is required — the owner uses their normal, intended `set_fixed_gas` capability. The only prerequisite is that the Silo is operating in fixed-gas mode, which is the defining feature of Silo deployments. Likelihood is **Medium** (requires a malicious Silo operator, but the protocol provides no protection once that assumption breaks).

---

### Recommendation

1. **Add a `max_fixed_gas: Option<u64>` field to `SubmitArgs`** (analogous to `max_gas_price`). During execution, if `fixed_gas > max_fixed_gas`, revert with an error before deducting any ETH.
2. **Enforce a timelock on `set_fixed_gas` / `set_silo_params`** so that changes to `fixed_gas` only take effect after a delay, giving users time to observe the change and avoid submitting transactions under the new value.

---

### Proof of Concept

**Setup**: Silo is running with `fixed_gas = 100_000`. A user submits a transaction with `gas_limit = 1_000_000` and `gas_price = 10 wei`, expecting to pay `100_000 * 10 = 1_000_000 wei`.

**Attack**:
1. Silo owner observes the user's pending `submit_with_args` transaction.
2. Owner submits `set_fixed_gas(fixed_gas: 1_000_000)` in the same NEAR block, ordered before the user's transaction.
3. User's transaction executes. `silo::get_fixed_gas(&io)` now returns `1_000_000`.
4. `charge_gas` computes `prepaid_amount = 1_000_000 * 10 = 10_000_000 wei` and deducts it from the user's balance.
5. `refund_unused_gas` computes `spent_amount = 1_000_000 * 10 = 10_000_000 wei` (using `fixed_gas`, not actual `gas_used`), so `refund = 0`.
6. Relayer (owner-controlled) receives `1_000_000 * priority_fee = excess ETH`.

**Net result**: User paid `10_000_000 wei` instead of `1_000_000 wei`. Owner extracted `9_000_000 wei` from the user.

**Relevant code locations**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** engine/src/engine.rs (L496-500)
```rust
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

**File:** engine/src/engine.rs (L1101-1101)
```rust
    let prepaid_amount = match engine.charge_gas(&sender, &transaction, max_gas_price, fixed_gas) {
```

**File:** engine/src/engine.rs (L1274-1291)
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
```

**File:** engine/src/lib.rs (L784-793)
```rust
    pub extern "C" fn set_fixed_gas() {
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: FixedGasArgs = io.read_input_borsh().sdk_unwrap();
        silo::set_fixed_gas(&mut io, args.fixed_gas);
    }
```

**File:** engine-types/src/parameters/engine.rs (L131-140)
```rust
/// Borsh-encoded submit arguments used by the `submit_with_args` function.
#[derive(Default, Debug, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
pub struct SubmitArgs {
    /// Bytes of the transaction.
    pub tx_data: Vec<u8>,
    /// Max gas price the user is ready to pay for the transaction.
    pub max_gas_price: Option<u128>,
    /// Address of the `ERC20` token the user prefers to pay in.
    pub gas_token_address: Option<Address>,
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L41-55)
```rust
pub fn get_fixed_gas<I: IO>(io: &I) -> Option<EthGas> {
    let key = fixed_gas_key();
    io.read_storage(&key)
        .and_then(|bytes| bytes.to_value().ok())
}

/// Set an amount of gas per transaction.
pub fn set_fixed_gas<I: IO>(io: &mut I, gas: Option<EthGas>) {
    let key = fixed_gas_key();

    if let Some(gas) = gas {
        io.write_borsh(&key, &gas);
    } else {
        io.remove_storage(&key);
    }
```
