### Title
Asymmetric `delta_gas` Adjustment in `compute_gas_refund` Silently Overcharges Users When Native Resource Consumption Falls Below EVM Gas Consumption — (`basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

`compute_gas_refund` adjusts `gas_used` upward when native resource consumption exceeds EVM gas consumption (`delta_gas > 0`), but never adjusts it downward when native resource consumption is lower (`delta_gas < 0`). The result is that users are overcharged: the excess fee is transferred to the operator rather than refunded to the sender. A TODO comment in the code explicitly acknowledges this gap.

---

### Finding Description

In `compute_gas_refund`, the dual-resource accounting model computes:

```
delta_gas = (native_used / native_per_gas) - gas_used
``` [1](#0-0) 

The documentation confirms the intended semantics:

> `deltaGas := (nativeUsed / nativePerGas) - gasUsed`
> If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. [2](#0-1) 

The code implements only the `delta_gas > 0` branch:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
}
// TODO: return delta_gas to gas_used?
``` [3](#0-2) 

When `delta_gas < 0` — meaning the user's native resource consumption is *less* than their EVM gas consumption implies — `gas_used` is not reduced. The user is therefore charged for more gas than their actual resource consumption justifies.

The resulting `gas_used` flows directly into the refund and operator-payment calculations:

- **Refund to user**: `gas_price * (gas_limit - gas_used)` — smaller than it should be
- **Payment to operator**: `gas_price * gas_used` — larger than it should be [4](#0-3) [5](#0-4) 

The user's pre-charged balance is debited in `precharge_fee`: [6](#0-5) 

The excess `gas_price * |delta_gas|` tokens are never returned to the sender.

---

### Impact Explanation

Users who submit transactions where EVM gas consumption is high but native resource consumption is low are overcharged. The overcharge amount is:

```
overcharge = gas_price * |delta_gas|
           = gas_price * (gas_used - native_used / native_per_gas)
```

These tokens are transferred to the operator rather than refunded to the user. This is a direct, permanent loss of user funds with no recovery path.

---

### Likelihood Explanation

The condition `delta_gas < 0` is triggered whenever a transaction's EVM gas consumption exceeds its native resource consumption (in gas-equivalent units). This is a realistic and common scenario for:

- Computation-heavy transactions with few storage writes (e.g., cryptographic operations, arithmetic loops)
- Any transaction where `native_used * native_price < gas_used * gas_price`

The `native_per_gas` ratio is `gas_price / native_price`. A user paying a high gas price relative to the native price will have a high `native_per_gas`, making it easier for `native_used / native_per_gas` to fall below `gas_used`. The condition is fully user-controllable via transaction parameters and calldata.

The `#[cfg(not(feature = "unlimited_native"))]` gate means this code path is active in all production configurations. [7](#0-6) 

---

### Recommendation

Apply the `delta_gas` adjustment symmetrically. When `delta_gas < 0`, reduce `gas_used` by `|delta_gas|`, subject to the `minimal_gas_used` floor:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else if delta_gas < 0 {
    let reduction = (-delta_gas) as u64;
    gas_used = gas_used.saturating_sub(reduction);
    gas_used = core::cmp::max(gas_used, minimal_gas_used);
}
```

This ensures users are refunded for unused native resource capacity, mirroring the symmetric treatment already applied in the `delta_gas > 0` direction.

---

### Proof of Concept

1. Deploy a contract that executes a computation-heavy loop (e.g., repeated `mulmod` or `addmod` opcodes) with **no storage writes**.
2. Submit a transaction calling this contract with a high `gas_price` (so `native_per_gas = gas_price / native_price` is large) and a generous `gas_limit`.
3. After execution:
   - `gas_used` (from ergs) will be high due to EVM computation.
   - `native_used` will be low because no pubdata was generated and proving cost is minimal.
   - `delta_gas = (native_used / native_per_gas) - gas_used` will be **negative**.
4. Observe that `gas_used` is **not** reduced. The user receives a smaller refund than their actual resource consumption justifies.
5. The operator receives `gas_price * gas_used` instead of `gas_price * (native_used / native_per_gas)`, capturing the overcharge.

The TODO comment at line 80 of `refund_calculation.rs` confirms the ZKsync OS developers are aware of this gap. [8](#0-7)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L66-81)
```rust
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

**File:** docs/double_resource_accounting.md (L47-52)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.

Finally, any remaining gas left is refunded as usual.
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L233-283)
```rust
    fn precharge_fee<Config: BasicBootloaderExecutionConfig>(
        system: &mut System<S>,
        transaction: &Transaction<<S as SystemTypes>::Allocator>,
        context: &mut Self::TransactionContext,
        _tracer: &mut impl Tracer<S>,
    ) -> Result<(), TxError> {
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
            })
            .map_err(|e| match e {
                SubsystemError::LeafUsage(interface_error) => {
                    unreachable!(
                        "balance should be pre-verified, but received error {:?}",
                        interface_error
                    );
                }
                SubsystemError::LeafDefect(internal_error) => internal_error.into(),
                // shouldn't be reachable as we are using infinite resources
                SubsystemError::LeafRuntime(runtime_error) => match runtime_error {
                    RuntimeError::FatalRuntimeError(_) => {
                        TxError::oon_as_validation(out_of_native_resources!().into())
                    }
                    RuntimeError::OutOfErgs(_) => {
                        TxError::Validation(InvalidTransaction::OutOfGasDuringValidation)
                    }
                },
                SubsystemError::Cascaded(cascaded_error) => match cascaded_error {},
            })?;
        Ok(())
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L452-488)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L514-516)
```rust
        let token_to_pay_operator = U256::from(context.gas_used)
            .checked_mul(gas_price_for_operator)
            .ok_or(internal_error!("gu*gpfo"))?;
```
