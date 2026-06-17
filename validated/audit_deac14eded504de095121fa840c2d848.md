### Title
Blob Gas Fee Charged from Sender but Never Transferred to Operator — (`basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs`)

### Summary

In `ZkTransactionFlowOnlyEOA`, the blob gas fee (`fee_for_blob_gas`) is included in `fee_to_prepay` and deducted from the sender during `precharge_fee`, but `refund_and_commit_fee` never transfers the blob fee portion to the operator/coinbase. The blob fee is effectively burned — a direct resource accounting bug where fees are computed, charged, and then lost.

### Finding Description

During ZK transaction validation (`zk/validation_impl.rs`), the blob gas fee is computed and added to `fee_to_prepay`:

```rust
let blob_gas_used = num_blobs as u64 * GAS_PER_BLOB;
let fee_for_blob_gas = if blob_gas_used > 0 {
    system.get_blob_base_fee_per_gas().checked_mul(U256::from(blob_gas_used))...
} else { U256::ZERO };
let fee_to_prepay = gas_fee_amount + fee_for_blob_gas;  // blob fee included
``` [1](#0-0) 

`precharge_fee` then deducts the full `fee_to_prepay` (including blob fee) from the sender:

```rust
let fee = context.fee_to_prepay;  // includes fee_for_blob_gas
system.io.update_account_nominal_token_balance(..., &from, &fee, true, ...)
``` [2](#0-1) 

However, `refund_and_commit_fee` only accounts for execution gas — the blob fee is never mentioned:

```rust
// Refund: gas_price * (gas_limit - gas_used)  — no blob gas refund
let token_to_refund = context.gas_price * U256::from(context.tx_gas_limit - context.gas_used);

// Operator: gas_used * gas_price_for_operator  — no blob gas fee
let token_to_pay_operator = U256::from(context.gas_used).checked_mul(gas_price_for_operator)?;
``` [3](#0-2) 

The blob fee is deducted from the sender but credited to no one. The accounting is:

| Party | Expected | Actual |
|---|---|---|
| Sender | Pays `gas_price * gas_used + blob_fee` | Pays `gas_price * gas_used + blob_fee` ✓ |
| Operator | Receives `gas_price_for_operator * gas_used + blob_fee` | Receives only `gas_price_for_operator * gas_used` ✗ |
| System | `blob_fee` burned | `blob_fee` silently destroyed ✗ |

The comment in the same function explicitly notes that only base fees are intentionally burned when `burn_base_fee` is enabled — there is no analogous intent for blob fees:

```rust
// EIP-1559 compatibility: When burn_base_fee is enabled, only priority fees
// go to the operator. Base fees are effectively "burned" (not transferred anywhere).
``` [4](#0-3) 

The same pattern exists in the Ethereum STF flow, but there blob fee burning is correct EIP-4844 behavior. In the ZK-specific flow, there is no protocol-level justification for burning blob fees.

### Impact Explanation

Every blob transaction processed through `ZkTransactionFlowOnlyEOA` causes the operator to lose the entire blob gas fee. The sender is charged correctly, but the operator receives nothing for blob gas. This is a direct, permanent loss of operator revenue — the blob fee tokens are destroyed from the system's total supply with no corresponding benefit.

### Likelihood Explanation

EIP-4844 is gated behind the `eip-4844` feature flag. Per `docs/execution_environments/evm.md` line 14, blob transactions are "not enabled in production." However, the `for_tests` and `eth_runner` feature sets in `forward_system/Cargo.toml` both enable `eip-4844`:

```toml
for_tests = ["production", "basic_bootloader/eip-4844"]
eth_runner = [..., "basic_bootloader/eip-4844"]
``` [5](#0-4) 

Any unprivileged user submitting a type-3 blob transaction in a configuration where `eip-4844` is active triggers this path. The attacker-controlled entry is simply submitting a valid blob transaction — no privilege required.

### Recommendation

In `refund_and_commit_fee` for `ZkTransactionFlowOnlyEOA`, transfer the blob fee to the coinbase/operator after execution, analogous to how execution gas fees are handled:

```rust
// After paying execution gas to operator:
if context.blob_gas_used > 0 {
    let blob_fee = U256::from(context.blob_gas_used)
        .checked_mul(system.get_blob_base_fee_per_gas())
        .ok_or(internal_error!("blob_gas * blob_fee"))?;
    // Transfer blob_fee to coinbase
    system.io.update_account_nominal_token_balance(
        ExecutionEnvironmentType::NoEE, &mut resources, &coinbase, &blob_fee, false, ...
    )?;
}
```

Alternatively, if blob fee burning is intentional for ZKsync ZK transactions (matching EIP-4844 semantics), this should be explicitly documented and the `blob_gas_used` field in `EthereumTxContext` should be used to verify the accounting is correct.

### Proof of Concept

1. Enable `eip-4844` feature (e.g., `for_tests` configuration).
2. Submit a ZKsync ZK transaction (type-3) with one or more blobs.
3. Observe that `fee_to_prepay` includes `fee_for_blob_gas` and is deducted from the sender.
4. After execution, check the operator/coinbase balance — it increases only by `gas_used * gas_price_for_operator`, with no blob fee component.
5. Check the sender balance — it decreased by `gas_price * gas_used + blob_fee`.
6. The `blob_fee` tokens are unaccounted for: not in the sender, not in the operator, not in any treasury. [6](#0-5) [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L464-489)
```rust
    // Note: no need to feature gate this part, as for non-EIP4844 transactions
    // num_blobs will be 0.
    let num_blobs = system.metadata.num_blobs();
    // NOTE: it's a special resource - not transaction gas. Will be used to charge fee only
    let blob_gas_used = num_blobs as u64 * GAS_PER_BLOB;
    let fee_for_blob_gas = if blob_gas_used > 0 {
        system_log!(
            system,
            "Blob gas price = {}\n",
            &system.get_blob_base_fee_per_gas()
        );

        let Some(value) = system
            .get_blob_base_fee_per_gas()
            .checked_mul(U256::from(blob_gas_used))
        else {
            return Err(TxError::Validation(
                InvalidTransaction::OverflowPaymentInTransaction,
            ));
        };

        value
    } else {
        U256::ZERO
    };
    let fee_to_prepay = gas_fee_amount
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L239-262)
```rust
        let from = transaction.from();
        let fee = context.fee_to_prepay;

        system_log!(
            system,
            "Will precharge {:?} native tokens for transaction\n",
            &fee
        );

        // ARCHITECTURE NOTE: Fee payment is split into two phases:
        // 1. Deduct full fee from sender at transaction start (here)
        // 2. Transfer actual payment to operator after execution (in refund_transaction_and_pay_operator)
        // This ensures sender has sufficient funds before execution begins
        context
            .intrinsic_resources
            .with_infinite_ergs(|resources| {
                system.io.update_account_nominal_token_balance(
                    ExecutionEnvironmentType::NoEE,
                    resources,
                    &from,
                    &fee,
                    true,
                    Config::SIMULATION,
                )
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L452-544)
```rust
        if context.tx_gas_limit > context.gas_used {
            system_log!(system, "Gas price for refund is {:?}\n", &context.gas_price);

            // refund
            let refund_recipient = transaction.from();
            let token_to_refund =
                context.gas_price * U256::from(context.tx_gas_limit - context.gas_used); // can not overflow

            // First refund the sender. Routed through `intrinsic_resources` so
            // the native charge (precharged by the intrinsic formula) can be
            // verified under `verify_intrinsic_native`.
            context
                .intrinsic_resources
                .with_infinite_ergs(|resources| {
                    system.io.update_account_nominal_token_balance(
                        ExecutionEnvironmentType::NoEE,
                        resources,
                        &refund_recipient,
                        &token_to_refund,
                        false,
                        Config::SIMULATION,
                    )
                })
                .map_err(|e| match e {
                    // Balance errors can not be cascaded
                    SubsystemError::Cascaded(CascadedError(inner, _)) => match inner {},
                    SubsystemError::LeafUsage(InterfaceError(ie, _)) => match ie {
                        BalanceError::InsufficientBalance => {
                            unreachable!("Cannot be insufficient when incrementing balance")
                        }
                        BalanceError::Overflow => {
                            interface_error!(BootloaderInterfaceError::CantPayRefundOverflow)
                        }
                    },
                    other => wrap_error!(other),
                })?;
        }

        // Next we pay the operator
        // ARCHITECTURE NOTE: Fee payment is split into two phases:
        // 1. Deduct full fee from sender at transaction start (in pay_for_transaction)
        // 2. Transfer actual payment to operator after execution (here)
        // This ensures sender has sufficient funds before execution begins

        // EIP-1559 compatibility: When burn_base_fee is enabled, only priority fees
        // go to the operator. Base fees are effectively "burned" (not transferred anywhere).
        let gas_price_for_operator = if cfg!(feature = "burn_base_fee") {
            let base_fee = system.get_eip1559_basefee();
            // We use saturating arithmetic to allow the caller of this method to
            // allow gas_price < base_fee. This can be used, for example, for
            // transaction simulation
            context.gas_price.saturating_sub(base_fee)
        } else {
            context.gas_price
        };

        system_log!(
            system,
            "Gas price for coinbase fee is {:?}\n",
            &gas_price_for_operator
        );

        let token_to_pay_operator = U256::from(context.gas_used)
            .checked_mul(gas_price_for_operator)
            .ok_or(internal_error!("gu*gpfo"))?;

        let coinbase = system.get_coinbase();
        // Operator payment native is precharged by the intrinsic formula too.
        context
            .intrinsic_resources
            .with_infinite_ergs(|resources| {
                system.io.update_account_nominal_token_balance(
                    ExecutionEnvironmentType::NoEE,
                    resources,
                    &coinbase,
                    &token_to_pay_operator,
                    false,
                    Config::SIMULATION,
                )
            })
            .map_err(|e| match e {
                // Balance errors can not be cascaded
                SubsystemError::Cascaded(CascadedError(inner, _)) => match inner {},
                SubsystemError::LeafUsage(InterfaceError(ie, _)) => match ie {
                    BalanceError::InsufficientBalance => {
                        unreachable!("Cannot be insufficient when incrementing balance")
                    }
                    BalanceError::Overflow => {
                        interface_error!(BootloaderInterfaceError::CantPayOperatorOverflow)
                    }
                },
                other => wrap_error!(other),
            })?;
```

**File:** forward_system/Cargo.toml (L52-55)
```text
for_tests = ["production", "basic_bootloader/eip-4844"]

# Features used for eth_runner
eth_runner = ["basic_bootloader/disable_system_contracts", "zk_ee/prevrandao", "unlimited_native","basic_bootloader/burn_base_fee", "basic_bootloader/eip-4844"]
```
