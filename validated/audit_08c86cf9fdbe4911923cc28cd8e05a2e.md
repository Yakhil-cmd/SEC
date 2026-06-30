Based on my analysis of the production code, this is a real vulnerability.

---

### Title
EIP-1559 `max_fee_per_gas=0` Bypasses Silo Fixed-Gas Fee Enforcement, Zeroing Relayer Yield — (`engine/src/engine.rs`)

### Summary

When silo mode is active (`fixed_gas = Some(N)`), an attacker can submit an EIP-1559 transaction with `max_fee_per_gas=0` and `max_priority_fee_per_gas=0`. Because Aurora's block base fee is zero, the early-return guard at `charge_gas` is not triggered (it requires `fixed_gas.is_none()`), but the subsequent fee computation still produces `effective_gas_price=0` and `prepaid_amount=0`. The transaction executes for free, and `refund_unused_gas` short-circuits on `effective_gas_price.is_zero()`, so the relayer receives no priority-fee yield despite `fixed_gas` being configured.

### Finding Description

**`charge_gas` early-return guard (lines 476–481):**

```rust
if transaction.max_fee_per_gas.is_zero()
    && fixed_gas.is_none()          // ← FALSE when silo mode active
    && block_base_fee_per_gas.is_zero()
{
    return Ok(GasPaymentResult::default());
}
``` [1](#0-0) 

When `fixed_gas = Some(N)`, the guard correctly does **not** early-return. However, execution falls through to the fee computation:

```rust
let priority_fee_per_gas = transaction.max_priority_fee_per_gas
    .min(transaction.max_fee_per_gas - block_base_fee_per_gas);
// = min(0, 0 - 0) = 0
let effective_gas_price = priority_fee_per_gas + block_base_fee_per_gas;
// = 0 + 0 = 0

let prepaid_amount = fixed_gas
    .map_or(transaction.gas_limit, EthGas::as_u256)
    .checked_mul(effective_gas_price)   // N * 0 = 0
    .map(Wei::new)
    ...;
``` [2](#0-1) 

`prepaid_amount = 0` and `effective_gas_price = 0` are returned. No balance is deducted from the sender.

**`refund_unused_gas` short-circuit (lines 1270–1272):**

```rust
if gas_result.effective_gas_price.is_zero() {
    return Ok(());
}
``` [3](#0-2) 

Because `effective_gas_price=0`, the function returns immediately. The relayer receives zero reward, and the sender receives zero refund (nothing was taken). The full `refund_unused_gas` reward path is dead. [4](#0-3) 

**No upstream guard prevents this.** The pre-`charge_gas` checks in the submit path only verify:
- `fixed_gas <= gas_limit` (line 1066)
- intrinsic gas coverage (line 1079)
- `max_priority_fee_per_gas <= max_fee_per_gas` (line 1083) — satisfied when both are 0 [5](#0-4) 

There is no check that rejects `max_fee_per_gas=0` when `fixed_gas.is_some()`.

### Impact Explanation

The `fixed_gas` silo feature is designed to enforce a minimum gas cost per transaction (e.g., `fixed_gas = 21000`). The invariant is that `relayer_balance_after − relayer_balance_before = fixed_gas × effective_gas_price > 0` whenever `fixed_gas > 0` and `gas_used > 0`. This invariant is broken: the relayer receives **zero** priority-fee yield on every transaction the attacker submits. This is **theft of unclaimed yield** (High severity per scope).

The existing test `test_relayer_balance_after_transfer` confirms the intended invariant — relayer receives `FIXED_GAS * ONE_GAS_PRICE` — but only tests with a non-zero gas price. [6](#0-5) 

### Likelihood Explanation

- Aurora's block base fee is 0 (no EIP-1559 base-fee burning on NEAR L2).
- EIP-1559 transactions with `max_fee_per_gas=0` are structurally valid; the only constraint is `max_priority_fee_per_gas <= max_fee_per_gas`, satisfied when both are 0.
- Any unprivileged EVM sender can craft such a transaction. No admin compromise required.
- The silo whitelist (if enabled) only gates *who* can submit, not *what fee* they pay.

### Recommendation

After computing `effective_gas_price`, add a guard that rejects the transaction when `fixed_gas` is set but the effective price is zero:

```rust
if fixed_gas.is_some() && effective_gas_price.is_zero() {
    return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee);
}
```

Alternatively, enforce a minimum `max_fee_per_gas > 0` at the submit-path level when `fixed_gas.is_some()`.

### Proof of Concept

Trace with `fixed_gas = Some(EthGas::new(21_000))`, `max_fee_per_gas = 0`, `max_priority_fee_per_gas = 0`, `block_base_fee_per_gas = 0`:

1. Guard at line 476: `true && false && true` → **not taken**.
2. Line 483: `0 < 0` → **false**, no error.
3. `priority_fee_per_gas = min(0, 0−0) = 0`.
4. `effective_gas_price = 0`.
5. `prepaid_amount = 21_000 × 0 = 0`. Sender balance unchanged.
6. EVM executes transaction normally.
7. `refund_unused_gas`: `effective_gas_price.is_zero()` → early return. Relayer balance unchanged.

The existing unit test `test_refund_fixed_gas_pays_expected_amount` (lines 2793–2817) demonstrates the correct path only with non-zero `effective_gas_price`; a parallel test with `effective_gas_price=0` and `fixed_gas=Some(...)` would confirm the broken invariant. [7](#0-6)

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

**File:** engine/src/engine.rs (L487-500)
```rust
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

**File:** engine/src/engine.rs (L1270-1272)
```rust
    if gas_result.effective_gas_price.is_zero() {
        return Ok(());
    }
```

**File:** engine/src/engine.rs (L1274-1300)
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

    if !refund.is_zero() {
        add_balance(io, sender, refund)?;
    }

    if !relayer_reward.is_zero() {
        add_balance(io, relayer, relayer_reward)?;
    }
```

**File:** engine/src/engine.rs (L2793-2818)
```rust
    #[test]
    fn test_refund_fixed_gas_pays_expected_amount() {
        let origin = Address::zero();
        let storage = RefCell::new(Storage::default());
        let mut io = StoragePointer(&storage);
        let expected_state = EngineState::default();
        state::set_state(&mut io, &expected_state).unwrap();
        let relayer = make_address(1, 1);
        let gas_result = GasPaymentResult {
            prepaid_amount: Wei::new_u64(8000),
            effective_gas_price: 1.into(),
            priority_fee_per_gas: 2.into(),
        };
        let gas_used = 4000;
        let fixed_gas = Some(EthGas::new(7000));

        refund_unused_gas(&mut io, &origin, gas_used, &gas_result, &relayer, fixed_gas).unwrap();

        let actual_refund = get_balance(&io, &origin);
        let expected_refund = Wei::new_u64(1000);
        assert_eq!(expected_refund, actual_refund);

        let actual_refund = get_balance(&io, &relayer);
        let expected_refund = Wei::new_u64(7000 * 2);
        assert_eq!(expected_refund, actual_refund);
    }
```

**File:** engine-tests/src/tests/silo.rs (L256-262)
```rust
    validate_address_balance_and_nonce(
        &runner,
        relayer,
        FIXED_GAS * ONE_GAS_PRICE,
        INITIAL_NONCE.into(),
    )
    .unwrap();
```
