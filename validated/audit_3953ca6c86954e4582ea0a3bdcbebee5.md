### Title
Asymmetric `delta_gas` Adjustment in `compute_gas_refund` Causes User Overcharge When Native Resource Consumption < EVM Gas Consumption — (`basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

In `compute_gas_refund`, the `delta_gas` correction is applied **only when positive** (native consumption exceeds EVM gas). When `delta_gas < 0` — meaning native resource consumption is less than EVM gas consumption — `gas_used` is never reduced. The user is therefore charged for more gas than their actual native resource consumption justifies, and the excess fee flows to the operator rather than being refunded to the sender. A TODO comment in the code explicitly acknowledges this asymmetry.

---

### Finding Description

`compute_gas_refund` in `refund_calculation.rs` computes a dual-resource reconciliation value:

```
delta_gas = (native_used / native_per_gas) - gas_used
``` [1](#0-0) 

When `delta_gas > 0` (native consumption exceeds EVM gas), `gas_used` is increased so the user pays for the extra native work. When `delta_gas < 0` (native consumption is *less* than EVM gas), the code does nothing — `gas_used` is left at the higher EVM-derived value. The comment `// TODO: return delta_gas to gas_used?` on line 80 explicitly flags this as an unresolved asymmetry. [2](#0-1) 

The fee settlement flow for ZK (L2) transactions is:

1. **Upfront deduction**: sender pays `gas_price × gas_limit` in `pay_for_transaction`.
2. **Refund**: sender receives back `gas_price × (gas_limit − gas_used)`.
3. **Operator payment**: coinbase receives `gas_price_for_operator × gas_used`. [3](#0-2) 

Because `gas_used` is not reduced when `delta_gas < 0`:

- The sender's refund is **smaller** than it should be by `|delta_gas| × gas_price`.
- The operator's payment is **larger** than it should be by the same amount.
- The discrepancy `|delta_gas| × gas_price` tokens are silently transferred from the user to the operator with no protocol justification.

The same `compute_gas_refund` function is called for both Ethereum-type and ZK-type L2 transactions: [4](#0-3) [5](#0-4) 

The double-resource accounting documentation confirms that only the positive `delta_gas` case is intended to be handled: [6](#0-5) 

---

### Impact Explanation

Any L2 transaction where EVM gas consumption exceeds native resource consumption results in the sender being overcharged. The excess `|delta_gas| × gas_price` tokens are transferred to the operator's coinbase address instead of being returned to the sender. This is a direct, quantifiable loss of base-token funds for the transaction sender. The magnitude scales with gas price and the size of the discrepancy; for transactions with many EVM-expensive but native-cheap operations (e.g., warm SLOAD-heavy loops), the overcharge can be substantial.

---

### Likelihood Explanation

The condition `delta_gas < 0` arises whenever a transaction consumes more EVM gas than its native resource consumption implies. This is a routine occurrence for:

- Transactions dominated by warm storage reads (`SLOAD` after EIP-2929 access-list warming), which are expensive in EVM gas but relatively cheap in native cycles.
- Transactions that use EVM gas refunds (SSTORE clearing), which reduce `gas_used` via the EIP-3529 refund counter but do not proportionally reduce native consumption.
- Any transaction where the `minimal_gas_used` floor (from validation) raises `gas_used` above the native-implied value.

No special privileges or unusual conditions are required; any unprivileged sender submitting a standard EVM transaction can encounter this path.

---

### Recommendation

When `delta_gas < 0`, reduce `gas_used` by `|delta_gas|` (subject to the `minimal_gas_used` floor) so that the sender is refunded the full amount corresponding to their actual resource consumption:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else if delta_gas < 0 {
    // Return unused gas to the user
    let reduction = (-delta_gas) as u64;
    gas_used = gas_used.saturating_sub(reduction).max(minimal_gas_used);
}
```

Remove the `// TODO: return delta_gas to gas_used?` comment once the fix is applied. [7](#0-6) 

---

### Proof of Concept

1. Deploy a contract containing a loop that performs many warm `SLOAD` operations (e.g., reading the same storage slot 500 times after an initial cold read).
2. Submit a ZK-type L2 transaction calling this contract with a non-zero `gas_price` and a `gas_limit` large enough to succeed.
3. After execution, observe `gas_used` reported in the `ZkTxResult`.
4. Independently compute `native_used / native_per_gas` from the `native_used` field and the block's `native_price`.
5. When `gas_used > native_used / native_per_gas`, the sender's refund is short by `(gas_used − native_used / native_per_gas) × gas_price` tokens, and the coinbase balance has increased by that same amount beyond what the operator earned for actual work.

The discrepancy is deterministic and reproducible for any transaction where warm-storage-heavy EVM execution dominates the gas profile.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L55-81)
```rust
    #[allow(unused_mut)]
    let mut gas_used = core::cmp::max(gas_used, minimal_gas_used);

    // Note: for zero gas price, we use "unlimited native"
    let full_native_limit = if cfg!(feature = "unlimited_native") || native_per_gas == 0 {
        u64::MAX - 1
    } else {
        gas_limit.saturating_mul(native_per_gas)
    };
    let native_used = full_native_limit.saturating_sub(resources.native().remaining().as_u64());

    #[cfg(not(feature = "unlimited_native"))]
    {
        // Adjust gas_used with difference with used native
        let delta_gas = if native_per_gas == 0 {
            0
        } else {
            (native_used / native_per_gas) as i64 - (gas_used as i64)
        };

        if delta_gas > 0 {
            // In this case, the native resource consumption is more than the
            // gas consumption accounted for. Consume extra gas.
            gas_used += delta_gas as u64;
        }
        // TODO: return delta_gas to gas_used?
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L427-434)
```rust
        let refund_info = compute_gas_refund(
            system,
            to_charge_for_pubdata,
            transaction.gas_limit(),
            min_gas_used,
            context.native_per_gas,
            &mut context.resources.main_resources,
        )?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L452-516)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs (L477-488)
```rust
        let refund_info = compute_gas_refund(
            system,
            S::Resources::empty(),
            transaction.gas_limit(),
            min_gas_used,
            0u64,
            &mut context.resources.main_resources,
        )?;
        context.gas_used = refund_info.gas_used;

        Ok(())
    }
```

**File:** docs/double_resource_accounting.md (L47-52)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.

Finally, any remaining gas left is refunded as usual.
```
