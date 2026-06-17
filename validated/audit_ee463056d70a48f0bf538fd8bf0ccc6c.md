### Title
Unvalidated `refund_recipient` in L1→L2 Priority Transactions Causes Permanent Fund Loss on Revert — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

When an L1→L2 priority transaction reverts on L2, the bootloader mints the entire deposit (minus operator fee) to the `refund_recipient` address stored in `transaction.reserved[1]`. This address is never validated: the structure-validation function contains an explicit `// TODO: validate address?` comment and performs no check. If a user submits a priority transaction with `refund_recipient = address(0)` — the default when the field is omitted — and the L2 execution reverts, the full deposited value is minted to the zero address and is permanently unrecoverable.

---

### Finding Description

The `validate_structure` function for ABI-encoded L1/upgrade transactions explicitly skips validation of `reserved[1]` (the `refund_recipient` field):

```rust
// reserved[1] = refund recipient for l1 to l2 and upgrade txs
match tx_type {
    Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
        // TODO: validate address?
    }
    _ => unreachable!(),
}
``` [1](#0-0) 

Later, in `process_l1_transaction`, when the L2 execution fails, the refund path computes `to_refund_recipient = total_deposited - pay_to_operator` (the full deposit minus fees) and unconditionally mints it to whatever address is in `reserved[1]`:

```rust
let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
mint_base_token::<S, Config>(
    system, system_functions, memories.reborrow(),
    &to_refund_recipient,
    &refund_recipient,   // ← never validated, can be address(0)
    ...
``` [2](#0-1) 

The same unvalidated address also receives the unused-gas refund on a successful execution: [3](#0-2) 

`transfer_from_treasury` performs a plain balance credit to the supplied address with no zero-address guard: [4](#0-3) 

The `L1TxBuilder` test helper defaults `refund_recipient` to `Address::default()` (zero address) when the caller omits it, confirming this is a realistic default: [5](#0-4) 

---

### Impact Explanation

When a user submits an L1→L2 priority transaction with `refund_recipient = address(0)` and the L2 execution reverts:

- The minting of `value` to `from` is rolled back (inside the execution frame).
- `total_deposited - pay_to_operator` — which equals `value + unused_gas_fee` — is minted to `address(0)`.
- The corresponding L1 funds remain locked in the bridge forever.
- There is no rescue path: `address(0)` has no private key and no deployed code on ZKsync OS.

Even on a successful execution, the unused-gas refund is silently sent to `address(0)`, burning those tokens.

**Impact**: Permanent, irrecoverable loss of user funds (base token) proportional to the deposited amount.

---

### Likelihood Explanation

- The `refund_recipient` field is an optional parameter in the L1 transaction format. Many bridge UIs and SDK helpers default it to `address(0)` when not explicitly provided (as confirmed by `L1TxBuilder::build` above).
- Any L2 execution that reverts — due to a contract bug, out-of-gas, or deliberate revert — triggers the loss path.
- No privileged access is required; any unprivileged user submitting a priority transaction is exposed.
- The `// TODO: validate address?` comment confirms the team is aware the field is unvalidated.

**Likelihood**: Medium — the scenario (omitted `refund_recipient` + L2 revert) is a common operational pattern.

---

### Recommendation

1. In `validate_structure`, reject L1/upgrade transactions where `reserved[1]` decodes to `address(0)`:

```rust
Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
    let recipient = self.reserved[1].read();
    if recipient.is_zero() {
        return Err(());
    }
}
```

2. As a defence-in-depth measure, add a zero-address guard in `process_l1_transaction` before calling `mint_base_token` for the refund:

```rust
let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
require_internal!(!refund_recipient.is_zero(), "refund_recipient is zero address", system)?;
```

3. Remove the `// TODO: validate address?` comment once the check is implemented.

---

### Proof of Concept

1. User submits an L1→L2 priority transaction on L1 with:
   - `to_mint = 1 ETH` (deposited and locked in L1 bridge)
   - `refund_recipient = address(0)` (omitted / defaulted)
   - `gas_limit = 100_000`, `gas_price = 1_000`
   - `calldata` that causes the L2 target contract to revert.

2. ZKsync OS bootloader processes the transaction:
   - `execute_l1_transaction_and_notify_result` returns `is_success = false`.
   - The execution frame is rolled back; the `value` mint to `from` is undone.
   - `pay_to_operator = gas_used * gas_price` is minted to coinbase.
   - `to_refund_recipient = total_deposited - pay_to_operator ≈ 1 ETH - fees`.

3. `refund_recipient = u256_to_b160_checked(reserved[1]) = address(0)`.

4. `mint_base_token(..., &to_refund_recipient, &address(0), ...)` credits ~1 ETH to `address(0)`.

5. The user's 1 ETH is permanently lost: locked on L1, minted to an inaccessible address on L2. [6](#0-5) [1](#0-0)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L312-360)
```rust
    let to_refund_recipient = if !is_success {
        // Upgrade transactions must always succeed
        if !is_priority_op {
            return Err(internal_error!("Upgrade transaction must succeed").into());
        }
        // If the transaction reverts, then the minting of the deposit
        // reverted too. Thus, we need to refund the entire deposit minus
        // the fee (`pay_to_operator`).
        total_deposited
            .checked_sub(pay_to_operator)
            .ok_or(internal_error!("td-pto"))
    } else {
        // If the transaction succeeds, then it is assumed that the
        // mint to `from` address was transferred correctly too.
        // In this case, we just refund the unused gas that the
        // transaction paid for initially.
        let prepaid_fee = gas_price
            .checked_mul(U256::from(transaction.gas_limit.read()))
            .ok_or(internal_error!("gp*gl"))?;
        prepaid_fee
            .checked_sub(pay_to_operator)
            .ok_or(internal_error!("pf-pto"))
    }?;
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
