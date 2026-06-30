The code trace is conclusive. Here is the full analysis:

---

### Title
Zero-Fee EVM Execution in Silo Mode via `max_fee_per_gas = 0` Bypasses `fixed_gas` Fee Enforcement — (`engine/src/engine.rs`)

### Summary

In silo mode with `fixed_gas = Some(N)`, an attacker can submit an EIP-1559 transaction with `max_fee_per_gas = 0` and `max_priority_fee_per_gas = 0`. The early-return guard in `charge_gas` does not fire (because `fixed_gas.is_none()` is `false`), but the subsequent `effective_gas_price` computation still resolves to `0`, making `prepaid_amount = fixed_gas * 0 = 0`. The sender pays nothing, the EVM executes the transaction in full, and the relayer receives zero fee reward.

### Finding Description

The vulnerability is in `Engine::charge_gas` at `engine/src/engine.rs`: [1](#0-0) 

**Step-by-step trace** with `fixed_gas = Some(N)`, `max_fee_per_gas = 0`, `max_priority_fee_per_gas = 0`, `block_base_fee_per_gas = 0` (Aurora default):

**Guard (lines 476–481):**
```
transaction.max_fee_per_gas.is_zero()  → TRUE
fixed_gas.is_none()                    → FALSE  ← blocks short-circuit
block_base_fee_per_gas.is_zero()       → TRUE
Combined: TRUE && FALSE && TRUE = FALSE → guard does NOT fire
```

The `fixed_gas.is_none()` condition was added to prevent the free-transaction short-circuit when silo mode is active. But it only prevents the early return — it does not enforce a non-zero price.

**Price computation (lines 487–493):**
```
priority_fee_per_gas = min(0, 0 - 0) = 0
effective_gas_price  = 0 + 0         = 0
```

**Prepaid amount (lines 496–500):**
```
prepaid_amount = fixed_gas.as_u256()   // = N (e.g. 1_000_000)
                 .checked_mul(0)        // N × 0 = 0
               = Wei(0)
```

The sender's balance is decremented by `Wei(0)` — no change. The function returns `GasPaymentResult { prepaid_amount: 0, effective_gas_price: 0, priority_fee_per_gas: 0 }`.

**EVM execution (lines 1116–1139):** The transaction executes normally with the full `gas_limit`.

**Refund (lines 1270–1272):**
```rust
if gas_result.effective_gas_price.is_zero() {
    return Ok(()); // no refund, no relayer reward
}
``` [2](#0-1) 

The relayer receives zero ETH reward. The NEAR gas cost of executing the transaction is borne entirely by the relayer with no compensation.

There is no minimum gas price check anywhere in the submit path: [3](#0-2) 

The only gas-price validation is `max_fee_per_gas >= block_base_fee_per_gas` (line 483), which is trivially satisfied when both are `0`.

### Impact Explanation

**Critical — Insolvency.** The silo operator sets `fixed_gas` to guarantee a minimum fee per transaction. This mechanism is completely defeated: any sender can execute arbitrary EVM code at zero cost. The relayer pays real NEAR gas for every such transaction and receives no ETH compensation. At scale, this drains the relayer's ETH balance and makes the silo economically insolvent.

### Likelihood Explanation

**High.** No special privileges are required. Any EOA whitelisted in the silo (or any sender if whitelisting is disabled) can craft a valid EIP-1559 transaction with `max_fee_per_gas = 0`. The transaction passes all validation checks: nonce, chain ID, intrinsic gas, `max_priority_fee_per_gas <= max_fee_per_gas` (both 0), and `fixed_gas <= gas_limit`. Aurora's `block_base_fee_per_gas` is `0` by default, making this trivially reachable.

### Recommendation

In `charge_gas`, when `fixed_gas.is_some()`, enforce that `effective_gas_price > 0` before computing `prepaid_amount`. One concrete fix: after computing `effective_gas_price`, if `fixed_gas.is_some() && effective_gas_price.is_zero()`, return `Err(GasPaymentError::MaxFeePerGasLessThanBaseFee)` (or a new dedicated error). This ensures the silo's fee invariant cannot be bypassed by a zero-price transaction.

### Proof of Concept

```rust
// In engine/src/engine.rs test module:
#[test]
fn test_silo_fixed_gas_zero_fee_bypass() {
    let origin = Address::zero();
    let current_account_id = AccountId::default();
    let env = Fixed::default();
    let storage = RefCell::new(Storage::default());
    let mut io = StoragePointer(&storage);
    // Sender has a balance
    add_balance(&mut io, &origin, Wei::new_u64(1_000_000)).unwrap();

    let mut engine: Engine<_, _> =
        Engine::new_with_state(EngineState::default(), origin, current_account_id, io, &env);

    let transaction = NormalizedEthTransaction {
        address: Address::default(),
        chain_id: None,
        nonce: U256::default(),
        gas_limit: 2_000_000.into(),
        max_priority_fee_per_gas: U256::zero(), // attacker sets 0
        max_fee_per_gas: U256::zero(),           // attacker sets 0
        to: None,
        value: Wei::default(),
        data: vec![],
        access_list: vec![],
        authorization_list: vec![],
    };

    // Silo mode: fixed_gas = Some(1_000_000)
    let fixed_gas = Some(EthGas::new(1_000_000));
    let result = engine.charge_gas(&origin, &transaction, None, fixed_gas).unwrap();

    // BUG: prepaid_amount is 0 even though fixed_gas is set
    assert_eq!(result.prepaid_amount, Wei::zero());
    assert_eq!(result.effective_gas_price, U256::zero());
    // Sender balance is unchanged — they paid nothing
}
```

This test passes on unmodified code, confirming the invariant `sender_balance_delta <= 0` is broken: the sender's balance is unchanged while the EVM executes their transaction.

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

**File:** engine/src/engine.rs (L1049-1106)
```rust
    let fixed_gas = silo::get_fixed_gas(&io);

    // Check if the sender has rights to submit transactions or deploy code.
    assert_access(&io, env, &transaction)?;

    // Validate the chain ID, if provided inside the signature:
    if let Some(chain_id) = transaction.chain_id
        && U256::from(chain_id) != U256::from_big_endian(&state.chain_id)
    {
        return Err(EngineErrorKind::InvalidChainId.into());
    }

    sdk::log!("signer_address {:?}", sender);

    check_nonce(&io, &sender, &transaction.nonce)?;

    // Check that fixed gas is not greater than the gas limit from the transaction.
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

    let mut engine: Engine<_, _, M> =
        Engine::new_with_state(state, sender, current_account_id, io, env);

    let sender_code = engine.code(sender.raw());
    // EIP-7702 - check if it's delegated designation. If it's a delegation designation, then,
    // even if `caller_code` is non-empty, the transaction should be executed.
    let is_delegated = Authorization::is_delegated(&sender_code);

    // EIP-3607: Reject transactions from senders with deployed code
    // EIP-7702: Accept transaction even if the caller has code.
    if !(sender_code.is_empty() || is_delegated) {
        return Err(EngineErrorKind::RejectCallerWithCode.into());
    }
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
