### Title
Silo Mode Fixed Gas Fee Bypassed via Zero `max_fee_per_gas` — (File: engine/src/engine.rs)

---

### Summary

In Silo mode, Aurora Engine enforces a `fixed_gas` fee per transaction. However, the fee is computed as `fixed_gas * effective_gas_price`. Because `block_base_fee_per_gas` is permanently hardcoded to zero and `effective_gas_price` is derived entirely from the user-controlled `max_fee_per_gas` field, any user can set `max_fee_per_gas = 0` in their signed transaction to make `effective_gas_price = 0`, resulting in zero fees paid. The guard that is supposed to short-circuit fee-free transactions explicitly excludes Silo mode, leaving the path open. Additionally, `submit_with_args` carries no caller access control, allowing any NEAR account to supply `max_gas_price = Some(0)` to achieve the same bypass even when the signed transaction carries a non-zero gas price.

---

### Finding Description

**Root cause — `charge_gas` in `engine/src/engine.rs` (lines 468–515)**

The fee charged to the sender is:

```
prepaid_amount = fixed_gas * effective_gas_price
```

where

```
effective_gas_price = priority_fee_per_gas + block_base_fee_per_gas
```

`block_base_fee_per_gas()` is hardcoded to `U256::zero()` and is never read from state:

```rust
fn block_base_fee_per_gas(&self) -> U256 {
    U256::zero()          // engine/src/engine.rs:1869-1871
}
```

`priority_fee_per_gas` is derived from the transaction's `max_fee_per_gas`:

```rust
let priority_fee_per_gas = transaction
    .max_priority_fee_per_gas
    .min(transaction.max_fee_per_gas - block_base_fee_per_gas);
// engine/src/engine.rs:487-489
```

When `max_fee_per_gas = 0` (user-controlled, part of the signed EVM transaction), `priority_fee_per_gas = 0`, so `effective_gas_price = 0`, and therefore `prepaid_amount = fixed_gas * 0 = 0`.

**The guard does not protect Silo mode**

The only guard that could short-circuit fee charging is:

```rust
if transaction.max_fee_per_gas.is_zero()
    && fixed_gas.is_none()          // ← false in Silo mode
    && block_base_fee_per_gas.is_zero()
{
    return Ok(GasPaymentResult::default());
}
// engine/src/engine.rs:476-481
```

In Silo mode `fixed_gas` is `Some(...)`, so `fixed_gas.is_none()` is `false`. The guard is never taken. Execution falls through to the fee calculation, which produces `prepaid_amount = 0`.

**Second attack vector — unrestricted `submit_with_args`**

`submit_with_args` in `engine/src/contract_methods/evm_transactions.rs` (lines 106–131) has no caller access control beyond `require_running`:

```rust
pub fn submit_with_args<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;          // only check
        let args: SubmitArgs = io.read_input_borsh()?;
        ...
    })
}
```

`SubmitArgs.max_gas_price` (`engine-types/src/parameters/engine.rs:137`) is passed directly into `charge_gas` at line 1100–1101:

```rust
let max_gas_price = args.max_gas_price.map(Into::into);
let prepaid_amount = match engine.charge_gas(&sender, &transaction, max_gas_price, fixed_gas) {
```

Inside `charge_gas`, `max_gas_price` caps `priority_fee_per_gas`:

```rust
let priority_fee_per_gas = max_gas_price.map_or(priority_fee_per_gas, |price| {
    price.min(priority_fee_per_gas)   // engine/src/engine.rs:490-492
});
```

Any NEAR account can call `submit_with_args` with `max_gas_price = Some(0)`, forcing `priority_fee_per_gas = 0` and therefore `prepaid_amount = 0`, even when the signed EVM transaction carries a non-zero `max_fee_per_gas`.

---

### Impact Explanation

In Silo mode, `fixed_gas` is the primary mechanism for charging users and compensating relayers. The relayer's EVM address receives `fixed_gas * effective_gas_price` as a reward via `refund_unused_gas`. When `effective_gas_price = 0`, `refund_unused_gas` returns immediately:

```rust
if gas_result.effective_gas_price.is_zero() {
    return Ok(());   // engine/src/engine.rs:1270-1272
}
```

The relayer receives nothing. The user retains the funds they should have paid as fees. This is a direct, repeatable theft of relayer yield with no preconditions beyond being a normal Aurora user.

**Impact: High — Theft of unclaimed yield (relayer fees).**

---

### Likelihood Explanation

Exploiting this requires no privileged access. Setting `max_fee_per_gas = 0` is a standard field in any EVM transaction (Legacy, EIP-1559, etc.) and requires only a valid ECDSA signature from the sender. Calling `submit_with_args` directly is equally trivial — it is a public NEAR contract method with no caller restriction. Any Aurora user who wishes to avoid fees can do so on every transaction in Silo mode.

**Likelihood: High.**

---

### Recommendation

1. **Enforce a minimum effective gas price in Silo mode.** When `fixed_gas` is `Some(...)`, reject transactions where `effective_gas_price == 0` before computing `prepaid_amount`.

2. **Decouple the Silo fee from the user-controlled gas price.** The `fixed_gas` fee should be a fixed Wei amount charged unconditionally, not `fixed_gas * gas_price`. This mirrors the intent described in CHANGES.md ("fixed cost per transaction") and prevents any gas-price manipulation from zeroing the fee.

3. **Restrict `submit_with_args` or validate `max_gas_price`.** If `max_gas_price` is intended to be a relayer-set cap, add a check that it cannot be set to zero when `fixed_gas` is active, or restrict the caller to registered relayers.

---

### Proof of Concept

**Attack path 1 — zero `max_fee_per_gas` in signed transaction (works via `submit`):**

1. Silo mode is active: `fixed_gas = 1_000_000`, relayer expects fee = `1_000_000 * gas_price`.
2. Attacker constructs a Legacy transaction with `gas_price = 0` (equivalently, EIP-1559 with `max_fee_per_gas = 0`).
3. Attacker calls `submit` with the signed transaction bytes.
4. Inside `charge_gas`:
   - `block_base_fee_per_gas = 0` (hardcoded)
   - Guard at line 476 not taken (`fixed_gas.is_some()`)
   - `priority_fee_per_gas = 0.min(0) = 0`
   - `effective_gas_price = 0`
   - `prepaid_amount = 1_000_000 * 0 = 0`
5. Attacker's EVM balance is unchanged. Relayer receives nothing.

**Attack path 2 — `max_gas_price = Some(0)` via `submit_with_args` (works even with non-zero tx gas price):**

1. Same Silo setup.
2. Attacker constructs a transaction with `max_fee_per_gas = 100` (non-zero).
3. Attacker calls `submit_with_args` with `SubmitArgs { tx_data: ..., max_gas_price: Some(0), gas_token_address: None }`.
4. Inside `charge_gas`:
   - `priority_fee_per_gas = 0.min(100) = 0`
   - `effective_gas_price = 0`
   - `prepaid_amount = 1_000_000 * 0 = 0`
5. Same outcome: zero fees paid, relayer receives nothing. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** engine/src/engine.rs (L1100-1101)
```rust
    let max_gas_price = args.max_gas_price.map(Into::into);
    let prepaid_amount = match engine.charge_gas(&sender, &transaction, max_gas_price, fixed_gas) {
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

**File:** engine/src/engine.rs (L1869-1871)
```rust
    fn block_base_fee_per_gas(&self) -> U256 {
        U256::zero()
    }
```

**File:** engine/src/contract_methods/evm_transactions.rs (L106-131)
```rust
pub fn submit_with_args<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let args: SubmitArgs = io.read_input_borsh()?;
        let current_account_id = env.current_account_id();
        let relayer_address = predecessor_address(&env.predecessor_account_id());
        let result = engine::submit(
            io,
            env,
            &args,
            state,
            current_account_id,
            relayer_address,
            handler,
        )?;
        let result_bytes = borsh::to_vec(&result).map_err(|_| errors::ERR_SERIALIZE)?;
        io.return_output(&result_bytes);

        Ok(result)
    })
}
```

**File:** engine-types/src/parameters/engine.rs (L132-140)
```rust
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

**File:** engine/src/contract_methods/silo/mod.rs (L40-56)
```rust
/// Return gas amount per transaction.
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
}
```
