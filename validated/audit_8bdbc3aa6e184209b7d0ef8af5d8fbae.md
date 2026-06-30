The exploit path is fully traceable and confirmed. Here is the complete analysis:

---

### Title
Free Transaction Execution via `max_gas_price=0` Bypasses Silo Fee in `charge_gas` — (`engine/src/engine.rs`)

### Summary

In silo mode with `fixed_gas` set, any user can call `submit_with_args` with `SubmitArgs { max_gas_price: Some(0), ... }`. Inside `charge_gas`, the `max_gas_price` cap collapses `priority_fee_per_gas` to zero, making `effective_gas_price = 0` and `prepaid_amount = 0`. The sender's balance is not reduced, and `refund_unused_gas` early-exits without paying the relayer. The transaction executes for free.

### Finding Description

**Entry point:** `submit_with_args` — a public, unrestricted NEAR contract method. [1](#0-0) 

`SubmitArgs.max_gas_price` is `Option<u128>` with no lower-bound validation. `Some(0)` is accepted without error. [2](#0-1) 

At line 1100, `args.max_gas_price.map(Into::into)` converts `Some(0u128)` to `Some(U256::zero())` and passes it directly to `charge_gas`. [3](#0-2) 

Inside `charge_gas`, the early-exit guard at lines 476–481 requires `fixed_gas.is_none()`. With `fixed_gas = Some(N)` (silo mode), this guard does **not** fire. [4](#0-3) 

The `max_gas_price` cap is then applied unconditionally via `.min()`:

```
priority_fee_per_gas = U256::zero().min(1000) = 0
``` [5](#0-4) 

Because `block_base_fee_per_gas()` is hardcoded to `U256::zero()`: [6](#0-5) 

`effective_gas_price = 0 + 0 = 0`, and therefore:

```
prepaid_amount = fixed_gas_N * 0 = Wei::zero()
``` [7](#0-6) 

`checked_sub(Wei::zero())` succeeds for any sender balance ≥ 0, so no `OutOfFund` error is raised and the sender's balance is unchanged. [8](#0-7) 

In `refund_unused_gas`, the `effective_gas_price.is_zero()` guard early-returns before any relayer reward is paid: [9](#0-8) 

### Impact Explanation

In silo mode with `fixed_gas` configured, the entire fee mechanism is bypassed. The sender pays zero ETH, the relayer receives zero reward, and the silo operator collects no fee revenue for any transaction submitted via `submit_with_args` with `max_gas_price: Some(0)`. This constitutes **insolvency**: the protocol cannot recover operating costs and relayers are not compensated.

### Likelihood Explanation

`submit_with_args` is a public NEAR contract method with no caller restrictions. Any EOA that can submit transactions (i.e., passes the whitelist check in silo mode) can exploit this. The `SubmitArgs` struct is Borsh-serialized by the caller, so `max_gas_price: Some(0)` requires only a trivially crafted input. No privileged access, leaked keys, or external oracle is needed.

### Recommendation

Add a lower-bound validation on `max_gas_price` before it is used as a cap. Specifically, in `charge_gas` (or at the `submit_with_args` entry point), reject or ignore `max_gas_price = Some(0)` when `fixed_gas` is set and the transaction's `max_fee_per_gas` is non-zero. A minimal fix:

```rust
// In charge_gas, after computing priority_fee_per_gas:
let priority_fee_per_gas = max_gas_price.map_or(priority_fee_per_gas, |price| {
    price.min(priority_fee_per_gas)
});
// Guard: if fixed_gas is set, effective_gas_price must be non-zero
if fixed_gas.is_some() && effective_gas_price.is_zero() && !transaction.max_fee_per_gas.is_zero() {
    return Err(GasPaymentError::MaxGasPriceTooLow);
}
```

Alternatively, treat `max_gas_price: Some(0)` the same as `None` (i.e., no cap) when `fixed_gas` is active.

### Proof of Concept

```rust
#[test]
fn test_free_execution_via_zero_max_gas_price_in_silo_mode() {
    use aurora_engine_types::types::EthGas;

    let origin = Address::zero();
    let current_account_id = AccountId::default();
    let env = Fixed::default();
    let storage = RefCell::new(Storage::default());
    let mut io = StoragePointer(&storage);

    let initial_balance = Wei::new_u64(1_000_000_000);
    add_balance(&mut io, &origin, initial_balance).unwrap();

    let mut engine: Engine<_, _> =
        Engine::new_with_state(EngineState::default(), origin, current_account_id, io, &env);

    let transaction = NormalizedEthTransaction {
        address: Address::default(),
        chain_id: None,
        nonce: U256::default(),
        gas_limit: 100_000.into(),
        max_priority_fee_per_gas: 1000.into(), // non-zero gas price
        max_fee_per_gas: 1000.into(),
        to: None,
        value: Wei::default(),
        data: vec![],
        access_list: vec![],
        authorization_list: vec![],
    };

    let fixed_gas = Some(EthGas::new(50_000)); // silo mode
    let max_gas_price = Some(U256::zero());     // attacker sets max_gas_price = 0

    let result = engine
        .charge_gas(&origin, &transaction, max_gas_price, fixed_gas)
        .unwrap();

    // Invariant violation: prepaid_amount should be > 0 for a non-zero gas_price tx
    assert_eq!(result.prepaid_amount, Wei::zero()); // BUG: fee is zero
    assert_eq!(result.effective_gas_price, U256::zero()); // BUG: price collapsed
}
```

This test passes on unmodified code, confirming the invariant `sender_balance_delta < 0` is violated for any executed transaction in silo mode when `max_gas_price = Some(0)` is supplied.

### Citations

**File:** engine/src/contract_methods/evm_transactions.rs (L106-130)
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

**File:** engine/src/engine.rs (L476-481)
```rust
        if transaction.max_fee_per_gas.is_zero()
            && fixed_gas.is_none()
            && block_base_fee_per_gas.is_zero()
        {
            return Ok(GasPaymentResult::default());
        }
```

**File:** engine/src/engine.rs (L490-493)
```rust
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

**File:** engine/src/engine.rs (L502-506)
```rust
        let new_balance = get_balance(&self.io, sender)
            .checked_sub(prepaid_amount)
            .ok_or(GasPaymentError::OutOfFund)?;

        set_balance(&mut self.io, sender, &new_balance);
```

**File:** engine/src/engine.rs (L1100-1106)
```rust
    let max_gas_price = args.max_gas_price.map(Into::into);
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

**File:** engine/src/engine.rs (L1869-1871)
```rust
    fn block_base_fee_per_gas(&self) -> U256 {
        U256::zero()
    }
```
