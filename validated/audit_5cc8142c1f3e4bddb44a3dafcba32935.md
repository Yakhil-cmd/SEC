### Title
L1→L2 Gas Refund Permanently Lost When `refund_recipient` Is Zero Address - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

### Summary

In `process_l1_transaction`, the gas refund for L1→L2 priority transactions is minted to `transaction.reserved[1]` (the `refund_recipient`) without any check that this address is non-zero. When `refund_recipient` is the zero address — which is the default when not explicitly set — the entire unused-gas refund is permanently transferred to `address(0)`, burning it.

### Finding Description

In `process_l1_transaction`, after execution completes, the bootloader computes `to_refund_recipient` (the unused gas refund) and unconditionally mints it to the address stored in `transaction.reserved[1]`:

```rust
// Line 336-359
if to_refund_recipient > U256::ZERO {
    let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
    mint_base_token::<S, Config>(
        system,
        system_functions,
        memories.reborrow(),
        &to_refund_recipient,
        &refund_recipient,   // ← no zero-address check
        ...
    )?;
}
``` [1](#0-0) 

There is no guard of the form `if refund_recipient != B160::ZERO`. The `mint_base_token` function calls `transfer_from_treasury`, which deducts from the treasury and credits the `to` address — if `to` is `B160::ZERO`, the tokens are credited to the zero address and are permanently inaccessible. [2](#0-1) 

The `refund_recipient` field defaults to `Address::default()` (zero address) in `L1TxBuilder::new()`: [3](#0-2) 

The `ZKsyncL1Tx` struct encodes `refund_recipient` into `reserved[1]` as a `U256`: [4](#0-3) 

The existing regression test explicitly uses `refund_recipient = address(0)` and **confirms** the refund is sent there, treating it as expected behavior: [5](#0-4) [6](#0-5) 

### Impact Explanation

Any L1→L2 priority transaction that:
1. Does not explicitly set a `refund_recipient` (defaults to zero address), **or**
2. Deliberately sets `refund_recipient = address(0)`

...and has unused gas after execution will have its entire gas refund (`(gas_limit - gas_used) * gas_price`) permanently burned. This is a direct, irreversible loss of base token funds for the transaction submitter. The treasury is debited, but the tokens go to an uncontrolled address.

**Impact: High** — direct loss of user funds (base token refund burned).

### Likelihood Explanation

**Likelihood: Medium-High.** The default `refund_recipient` in `L1TxBuilder` is the zero address, and the `ZKsyncL1Tx` struct's `refund_recipient` field defaults to `Address::default()`. Any L1→L2 transaction submitted without explicitly setting a refund recipient will trigger this path whenever there is unused gas. This is a common scenario — most L1→L2 transactions do not consume exactly their gas limit.

### Recommendation

Before calling `mint_base_token` for the refund, add a zero-address guard. If `refund_recipient` is zero, either skip the refund (leaving it in the treasury) or redirect it to the transaction sender (`transaction.from.read()`):

```rust
if to_refund_recipient > U256::ZERO {
    let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
    if refund_recipient != B160::ZERO {
        mint_base_token::<S, Config>(
            system,
            system_functions,
            memories.reborrow(),
            &to_refund_recipient,
            &refund_recipient,
            ...
        )?;
    }
    // else: leave in treasury or redirect to `from`
}
```

### Proof of Concept

The existing test `test_treasury_based_token_distribution_regression` already demonstrates the bug: [5](#0-4) 

It sets `refund_recipient = address(0)`, executes an L1→L2 transaction with `gas_limit = 100_000` and `gas_price = 1000`, and then at lines 1937–1943 asserts that `total_to_refund_recipient` (= `(100_000 - gas_used) * 1000`) was credited to the zero address — confirming the refund is burned rather than returned to the sender. [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L336-360)
```rust
    if to_refund_recipient > U256::ZERO {
        let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
        mint_base_token::<S, Config>(
            system,
            system_functions,
            memories.reborrow(),
            &to_refund_recipient,
            &refund_recipient,
            l1_chain_id,
            &mut inf_resources,
            tracer,
            validator,
        )
        .map_err(|e| -> BootloaderSubsystemError {
            match e.root_cause() {
                RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
                    internal_error!("Out of ergs on infinite ergs").into()
                }
                RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => {
                    internal_error!("Out of native on infinite").into()
                }
                _ => e,
            }
        })?;
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L768-769)
```rust
    transfer_from_treasury::<S>(system, amount, to, resources, Config::SIMULATION)
}
```

**File:** tests/rig/src/utils/mod.rs (L334-334)
```rust
            refund_recipient: Default::default(),
```

**File:** tests/common/src/zksync_tx/l1_tx.rs (L60-64)
```rust
        let refund_recipient: U160 = self.refund_recipient.into();
        let reserved = [
            self.to_mint,
            U256::from(refund_recipient),
            U256::ZERO,
```

**File:** tests/instances/transactions/src/lib.rs (L1843-1843)
```rust
    let refund_recipient = address!("0000000000000000000000000000000000000000"); // refund recipient (zero address)
```

**File:** tests/instances/transactions/src/lib.rs (L1907-1943)
```rust
    // Calculate total amount that should go to operator (fee + refund)
    // Refund recipient is 0 in this test
    let gas_limit = 100_000u64;
    let gas_refund = gas_limit - gas_used;
    let refund_amount = U256::from(gas_refund) * U256::from(gas_price);
    let total_to_operator = fee_paid_to_operator;
    let total_to_refund_recipient = refund_amount;

    // Verify treasury balance decreased by max fee (fees + refund)
    let treasury_decrease = treasury_initial_balance - treasury_final_balance;
    let expected_treasury_decrease = total_to_operator + total_to_refund_recipient;
    assert_eq!(
        treasury_decrease, expected_treasury_decrease,
        "Treasury should decrease by total operator payment plus refund and value transferred"
    );

    // Verify operator received total payment from treasury (fee + refund)
    let operator_increase = operator_final_balance - operator_initial_balance;
    assert_eq!(
        operator_increase, total_to_operator,
        "Operator should receive fee + refund from treasury"
    );

    // Verify recipient received value from treasury (not minted)
    let recipient_increase = recipient_final_balance - recipient_initial_balance;
    assert_eq!(
        recipient_increase, value_to_transfer,
        "Recipient should receive exact value amount from treasury"
    );

    // Verify refund recipient received value from treasury (not minted)
    let refund_recipient_increase =
        refund_recipient_final_balance - refund_recipient_initial_balance;
    assert_eq!(
        refund_recipient_increase, total_to_refund_recipient,
        "Refund recipient should receive correct refund amount from treasury"
    );
```
