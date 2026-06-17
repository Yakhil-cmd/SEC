### Title
Zero-Address Refund Recipient in L1→L2 Transactions Burns Gas Refunds Permanently - (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

### Summary
L1→L2 priority transactions carry a `refund_recipient` field (`reserved[1]`) that receives the unused-gas refund after execution. The `validate_structure()` function explicitly skips zero-address validation for this field with a `// TODO: validate address?` comment. When `reserved[1]` is zero (the default value), the bootloader mints the refund amount to `address(0)`, permanently burning those tokens.

### Finding Description

The ABI-encoded transaction validator in `validate_structure()` checks many fields but explicitly leaves the refund recipient unvalidated: [1](#0-0) 

After execution, `process_l1_transaction` reads `reserved[1]` directly and passes it to `mint_base_token` without any zero-address guard: [2](#0-1) 

`mint_base_token` calls `transfer_from_treasury`, which credits the `to` address unconditionally: [3](#0-2) [4](#0-3) 

The `ZKsyncL1Tx` struct's `refund_recipient` field defaults to `Address::default()` (zero address), and `L1TxBuilder::build()` uses `unwrap_or_default()` for it: [5](#0-4) 

The existing regression test explicitly confirms this behavior — it sets `refund_recipient = address(0)` and asserts the refund amount is credited there: [6](#0-5) [7](#0-6) 

### Impact Explanation

Any L1→L2 transaction submitted with `reserved[1] = 0` (the zero address) will have its entire gas refund (`(gas_limit - gas_used) × gas_price`) permanently burned to `address(0)`. These tokens are deducted from the treasury and credited to an uncontrolled address from which they can never be recovered. The loss scales with `gas_limit - gas_used`: a transaction with a large gas limit and low actual consumption loses the most. Since `address(0)` is the default value for the `refund_recipient` field, any L1 sender who omits the field (or uses a default-constructed transaction) silently burns their refund.

### Likelihood Explanation

The `refund_recipient` field defaults to `address(0)` in the `ZKsyncL1Tx` struct and in `L1TxBuilder`. Any L1 contract or bridge that does not explicitly populate this field will trigger the loss on every transaction. The entry path requires no privilege — any L1→L2 transaction sender can trigger it, either accidentally (by omission) or deliberately (to grief the treasury). The `// TODO: validate address?` comment confirms the developers are aware the check is missing.

### Recommendation

In `validate_structure()`, reject L1→L2 and upgrade transactions whose `reserved[1]` decodes to the zero address:

```rust
// reserved[1] = refund recipient for l1 to l2 and upgrade txs
match tx_type {
    Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
        let refund_recipient = u256_to_b160_checked(self.reserved[1].read());
        if refund_recipient == B160::ZERO {
            return Err(());
        }
    }
    _ => unreachable!(),
}
```

Alternatively, fall back to `transaction.from()` when `reserved[1]` is zero, matching the behavior of the ZKsync Era legacy bootloader.

### Proof of Concept

1. Submit an L1→L2 transaction with `reserved[1] = U256::ZERO` (i.e., `refund_recipient = address(0)`), a large `gas_limit` (e.g., 1,000,000), and a low-cost call body (e.g., empty calldata to an EOA).
2. The transaction executes successfully, consuming only ~21,000 gas.
3. `process_l1_transaction` computes `to_refund_recipient = (1_000_000 - 21_000) × gas_price > 0`.
4. `mint_base_token` is called with `to = B160::ZERO`.
5. `transfer_from_treasury` deducts the refund from the treasury and credits `address(0)`.
6. The refund tokens are permanently inaccessible.

The existing test `test_treasury_based_token_distribution_regression` in `tests/instances/transactions/src/lib.rs` already demonstrates this exact flow with `refund_recipient = address(0)` and asserts the refund is credited there, confirming the vulnerability is present and exercised. [1](#0-0) [2](#0-1)

### Citations

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L267-273)
```rust
        // reserved[1] = refund recipient for l1 to l2 and upgrade txs
        match tx_type {
            Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
                // TODO: validate address?
            }
            _ => unreachable!(),
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L335-360)
```rust
    // Mint refund portion of the deposit to the refund recipient.
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L813-831)
```rust
    let _ = system
        .io
        .update_account_nominal_token_balance(
            zk_ee::execution_environment_type::ExecutionEnvironmentType::EVM,
            resources,
            to,
            nominal_token_value,
            false, // false = add to balance
            fee_payment_in_simulation,
        )
        .map_err(|e| -> BootloaderSubsystemError {
            match e {
                SubsystemError::LeafUsage(balance_error) => {
                    system_log!(system, "Error while minting: {balance_error:?}");
                    interface_error!(BootloaderInterfaceError::MintingBalanceOverflow)
                }
                _ => wrap_error!(e),
            }
        })?;
```

**File:** tests/rig/src/utils/mod.rs (L409-409)
```rust
            refund_recipient: self.refund_recipient.unwrap_or_default(),
```

**File:** tests/instances/transactions/src/lib.rs (L1843-1843)
```rust
    let refund_recipient = address!("0000000000000000000000000000000000000000"); // refund recipient (zero address)
```

**File:** tests/instances/transactions/src/lib.rs (L1938-1943)
```rust
    let refund_recipient_increase =
        refund_recipient_final_balance - refund_recipient_initial_balance;
    assert_eq!(
        refund_recipient_increase, total_to_refund_recipient,
        "Refund recipient should receive correct refund amount from treasury"
    );
```
