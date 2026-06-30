The code path is fully traceable. Let me confirm the exact logic in `submit_with_alt_modexp` where `charge_gas` is called.

The exploit path is fully confirmed. Here is the complete analysis:

---

### Title
Zero `effective_gas_price` Bypass in Silo Mode Allows Free Transaction Execution — (`engine/src/engine.rs`)

### Summary

In silo mode, the early-return guard in `Engine::charge_gas` requires `fixed_gas.is_none()` as one of three conjunctive conditions. When `fixed_gas` is set (silo mode active), this guard never fires even when `max_fee_per_gas = 0` and `block_base_fee_per_gas = 0`, causing `effective_gas_price` to be computed as zero. The result is `prepaid_amount = fixed_gas * 0 = 0`: the sender pays nothing, and `refund_unused_gas` short-circuits on `effective_gas_price.is_zero()`, so the relayer receives nothing either.

### Finding Description

**Entry point:** `submit` / `submit_with_alt_modexp` in `engine/src/engine.rs`, reachable by any EVM user via the public `submit` NEAR contract method.

**Step 1 — `fixed_gas` is loaded from storage:** [1](#0-0) 

`fixed_gas = Some(1_000_000)` when silo params have been set.

**Step 2 — `FixedGasOverflow` guard passes** if the attacker sets `gas_limit >= fixed_gas`: [2](#0-1) 

**Step 3 — `charge_gas` is called with `fixed_gas = Some(...)`:** [3](#0-2) 

**Step 4 — The early-return guard is NOT triggered.** The condition requires all three to be true simultaneously: [4](#0-3) 

With `fixed_gas.is_some()`, the middle clause `fixed_gas.is_none()` is **false**, so the guard never fires — even though `max_fee_per_gas = 0` and `block_base_fee_per_gas = 0`.

**Step 5 — `effective_gas_price` computes to zero:** [5](#0-4) 

`priority_fee_per_gas = min(0, 0 − 0) = 0`, so `effective_gas_price = 0 + 0 = 0`.

**Step 6 — `prepaid_amount = 0`, balance unchanged:** [6](#0-5) 

`fixed_gas.as_u256() * 0 = 0`. The `checked_sub(0)` on the sender's balance always succeeds, even with a zero ETH balance.

**Step 7 — EVM executes normally** (call or deploy proceeds with full `gas_limit`).

**Step 8 — `refund_unused_gas` short-circuits, relayer receives nothing:** [7](#0-6) 

Because `effective_gas_price = 0`, the function returns immediately without crediting the relayer.

### Impact Explanation

The silo operator sets `fixed_gas` precisely to guarantee that every accepted transaction pays `fixed_gas * gas_price` to the relayer. This invariant is completely broken: any unprivileged EVM user can submit transactions with `max_fee_per_gas = 0` and execute them for free. The relayer consumes NEAR gas to process each transaction but receives zero ETH compensation. Repeated exploitation creates a persistent, unbounded deficit between expected fee revenue and actual ETH collected — a protocol insolvency condition.

### Likelihood Explanation

- No privilege is required; any EVM address can submit a transaction with `max_fee_per_gas = 0`.
- Aurora's `block_base_fee_per_gas` is 0 by default, satisfying the remaining precondition automatically.
- The exploit is deterministic and repeatable with no rate limit.
- The only prerequisite is that the silo operator has called `set_silo_params` with a non-zero `fixed_gas`, which is the normal operational state for any silo deployment.

### Recommendation

The early-return guard's logic is inverted for the silo case. The fix is to reject (or short-circuit to free) when `effective_gas_price` would be zero regardless of `fixed_gas`. Two concrete options:

1. **Reject zero-price transactions in silo mode:** After computing `effective_gas_price`, add:
   ```rust
   if effective_gas_price.is_zero() && fixed_gas.is_some() {
       return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee);
   }
   ```

2. **Fix the early-return condition** to not require `fixed_gas.is_none()` when both fee fields are zero:
   ```rust
   if transaction.max_fee_per_gas.is_zero() && block_base_fee_per_gas.is_zero() {
       if fixed_gas.is_none() {
           return Ok(GasPaymentResult::default());
       }
       return Err(GasPaymentError::MaxFeePerGasLessThanBaseFee);
   }
   ```

### Proof of Concept

```rust
// In engine-tests/src/tests/silo.rs (local unit test, no mainnet)
#[test]
fn test_free_tx_with_zero_max_fee_in_silo_mode() {
    let (mut runner, mut signer, receiver) = initialize_transfer();
    let sender = utils::address_from_secret_key(&signer.secret_key);

    // Activate silo mode with fixed_gas = 1_000_000
    set_silo_params(&mut runner, Some(SiloParamsArgs {
        fixed_gas: EthGas::new(1_000_000),
        erc20_fallback_address: ERC20_FALLBACK_ADDRESS,
    }));

    let relayer = sdk::types::near_account_to_evm_address(
        runner.context.predecessor_account_id.as_bytes()
    );
    let relayer_balance_before = runner.get_balance(relayer);
    let sender_balance_before = runner.get_balance(sender);

    // Submit EIP-1559 tx with max_fee_per_gas = 0, max_priority_fee_per_gas = 0
    let result = runner.submit_with_signer(&mut signer, |nonce| {
        let mut tx = utils::transfer(receiver, Wei::new_u64(1_000), nonce);
        tx.gas_limit = 2_000_000.into(); // >= fixed_gas, passes FixedGasOverflow check
        tx.gas_price = 0.into();         // max_fee_per_gas = 0
        tx
    }).unwrap();

    assert!(matches!(result.status, TransactionStatus::Succeed(_)));

    // Sender paid nothing for gas
    assert_eq!(runner.get_balance(sender), sender_balance_before - Wei::new_u64(1_000));
    // Relayer received nothing
    assert_eq!(runner.get_balance(relayer), relayer_balance_before);
    // Invariant violated: relayer should have received fixed_gas * price > 0
}
```

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

**File:** engine/src/engine.rs (L496-506)
```rust
        let prepaid_amount = fixed_gas
            .map_or(transaction.gas_limit, EthGas::as_u256)
            .checked_mul(effective_gas_price)
            .map(Wei::new)
            .ok_or(GasPaymentError::EthAmountOverflow)?;

        let new_balance = get_balance(&self.io, sender)
            .checked_sub(prepaid_amount)
            .ok_or(GasPaymentError::OutOfFund)?;

        set_balance(&mut self.io, sender, &new_balance);
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

**File:** engine/src/engine.rs (L1101-1106)
```rust
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
