### Title
Unvalidated Zero-Address `refund_recipient` in L1→L2 Transactions Silently Burns Refund Funds - (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The `process_l1_transaction` function in ZKsync OS processes L1→L2 priority transactions. After execution, it mints a gas refund to the address stored in `transaction.reserved[1]` (the `refund_recipient` field). This field is never validated against `address(0)`. When `refund_recipient` is `address(0)`, the refund tokens are silently transferred to the zero address — permanently burning them — with no error, no revert, and no warning. The L1 submitter loses the refunded gas funds with no recourse.

---

### Finding Description

In `process_l1_transaction`, after computing `to_refund_recipient`, the code reads the refund recipient address directly from the transaction's `reserved[1]` field without any zero-address check:

```rust
// process_l1_transaction.rs line 337
let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
mint_base_token::<S, Config>(
    system,
    system_functions,
    memories.reborrow(),
    &to_refund_recipient,
    &refund_recipient,   // <-- can be address(0)
    ...
)?;
```

The `validate_structure` function in `AbiEncodedTransaction` explicitly leaves this field unvalidated with a `TODO` comment:

```rust
// reserved[1] = refund recipient for l1 to l2 and upgrade txs
match tx_type {
    Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
        // TODO: validate address?
    }
    ...
}
```

The downstream `transfer_from_treasury` function calls `update_account_nominal_token_balance` with the zero address as recipient. The system's balance update function does not reject zero-address recipients — it simply increments the balance of `address(0)`. Those tokens are permanently inaccessible (burned).

The `L1TxBuilder` test helper defaults `refund_recipient` to `Address::default()` (zero address) when not explicitly set, and the existing regression test `test_treasury_based_token_distribution_regression` explicitly uses `refund_recipient = address(0)` and asserts that the refund amount is correctly credited to it — confirming this path is live and accepted by the system.

---

### Impact Explanation

An L1 user who submits a priority transaction with `refund_recipient = address(0)` (either by mistake or because the L1 bridge contract defaults to zero when the field is omitted) will have their entire unused-gas refund permanently burned. The refund amount can be substantial: `(gas_limit - gas_used) * gas_price`. For a transaction with `gas_limit = 1,000,000` and `gas_price = 1000`, the refund could be hundreds of thousands of wei or more. The funds are deducted from the treasury (the L1 bridge deposit), transferred to `address(0)`, and are unrecoverable. This is a direct, permanent loss of user funds with no revert or error signal.

---

### Likelihood Explanation

This is highly likely to occur in practice. The `L1TxBuilder` helper defaults `refund_recipient` to `Address::default()` (zero) when not set. Any L1 bridge integration that omits the `refund_recipient` field, or any user who submits a priority transaction without explicitly specifying a refund recipient, will trigger this path. The existing test suite already exercises this exact scenario and treats it as correct behavior, meaning there is no existing protection.

---

### Recommendation

1. In `validate_structure` inside `AbiEncodedTransaction`, reject L1/upgrade transactions where `reserved[1]` decodes to `B160::ZERO` (resolve the `TODO: validate address?` comment).
2. Alternatively, add a zero-address guard in `process_l1_transaction` before calling `mint_base_token` for the refund: if `refund_recipient == B160::ZERO`, either skip the refund mint (burning the funds intentionally as a documented choice) or redirect to `transaction.from`.
3. Update `L1TxBuilder::build` to require an explicit `refund_recipient` rather than defaulting to zero.

---

### Proof of Concept

1. Submit an L1→L2 priority transaction with `refund_recipient = address(0)`, `gas_limit = 100_000`, `gas_price = 1000`, and a simple call that uses only 50,000 gas.
2. `to_refund_recipient = (100_000 - 50_000) * 1000 = 50_000_000` tokens.
3. `refund_recipient = u256_to_b160_checked(0) = B160::ZERO`.
4. `mint_base_token` is called with `to = B160::ZERO`.
5. `transfer_from_treasury` subtracts 50,000,000 from the treasury and adds it to `address(0)`.
6. The 50,000,000 tokens are permanently burned. The L1 depositor receives nothing.

This is confirmed by the existing test at `tests/instances/transactions/src/lib.rs:1843` which sets `refund_recipient = address(0)` and verifies the refund amount is credited there — demonstrating the system accepts and processes this without error. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L776-831)
```rust
pub fn transfer_from_treasury<'a, S: EthereumLikeTypes + 'a>(
    system: &mut System<S>,
    nominal_token_value: &U256,
    to: &B160,
    resources: &mut S::Resources,
    fee_payment_in_simulation: bool,
) -> Result<(), BootloaderSubsystemError>
where
    S::IO: IOSubsystemExt,
{
    system_log!(
        system,
        "Transferring {nominal_token_value:?} tokens from treasury to {to:?}\n"
    );

    let treasury_address = &system_hooks::addresses_constants::BASE_TOKEN_HOLDER_ADDRESS;

    let _ = system
        .io
        .update_account_nominal_token_balance(
            zk_ee::execution_environment_type::ExecutionEnvironmentType::EVM,
            resources,
            treasury_address,
            nominal_token_value,
            true, // true = subtract from balance
            fee_payment_in_simulation,
        )
        .map_err(|e| -> BootloaderSubsystemError {
            match e {
                SubsystemError::LeafUsage(balance_error) => {
                    system_log!(system, "Treasury transfer failed: {balance_error:?}");
                    interface_error!(BootloaderInterfaceError::TreasuryTransferFailed)
                }
                _ => wrap_error!(e),
            }
        })?;

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

**File:** tests/instances/transactions/src/lib.rs (L1843-1843)
```rust
    let refund_recipient = address!("0000000000000000000000000000000000000000"); // refund recipient (zero address)
```

**File:** tests/rig/src/utils/mod.rs (L409-409)
```rust
            refund_recipient: self.refund_recipient.unwrap_or_default(),
```
