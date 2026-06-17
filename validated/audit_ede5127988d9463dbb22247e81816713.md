### Title
Unused Native Resources Not Refunded to User in `compute_gas_refund` — (`basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

`compute_gas_refund` adjusts `gas_used` upward when native resource consumption exceeds EVM gas consumption (`delta_gas > 0`), but never adjusts it downward when native consumption is *less* than gas consumption (`delta_gas < 0`). The result is that a user who consumed fewer native resources than their EVM gas implies is silently overcharged: the excess fee is transferred to the operator rather than refunded to the sender. This is a direct, user-facing loss of base tokens with no privileged precondition.

---

### Finding Description

`compute_gas_refund` in `refund_calculation.rs` implements ZKsync OS's double-resource accounting reconciliation step. After execution, it computes:

```
delta_gas = (native_used / native_per_gas) - gas_used
``` [1](#0-0) 

The design intent, documented in `docs/double_resource_accounting.md`, is:

> "If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee." [2](#0-1) 

The code correctly handles `delta_gas > 0` by increasing `gas_used`:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
}
// TODO: return delta_gas to gas_used?
``` [3](#0-2) 

However, when `delta_gas < 0` — meaning the transaction consumed *less* native resource than its EVM gas consumption implies — `gas_used` is **not** reduced. The `// TODO: return delta_gas to gas_used?` comment is an explicit acknowledgment that this case is unresolved.

This inflated `gas_used` propagates directly into the refund and fee-payment steps for both L2 and L1 transactions:

**L2 transactions** (`ZkTransactionFlowOnlyEOA::refund_and_commit_fee`):
- User refund = `gas_price × (gas_limit − gas_used)` — smaller than it should be
- Operator payment = `gas_used × gas_price_for_operator` — larger than it should be [4](#0-3) 

**L1 transactions** (`process_l1_transaction`):
- `pay_to_operator = gas_used × gas_price` — inflated
- `to_refund_recipient = prepaid_fee − pay_to_operator` — deflated [5](#0-4) 

The entire `delta_gas` adjustment block is guarded by `#[cfg(not(feature = "unlimited_native"))]`, so it is active in production (where native resources are finite) but absent in test configurations that enable `unlimited_native`. [1](#0-0) 

---

### Impact Explanation

For every transaction where `native_used / native_per_gas < gas_used`, the sender loses:

```
|delta_gas| × gas_price  base tokens
```

These tokens are not burned; they are silently redirected to the operator (and partially burned as base fee when `burn_base_fee` is enabled). The user has no way to recover them. For L1 deposits, the refund recipient receives less than the unused-gas portion of `total_deposited` warrants.

In the extreme case where native consumption is zero, `delta_gas = −gas_used`, meaning the user could lose their entire gas fee.

---

### Likelihood Explanation

`delta_gas < 0` occurs whenever a transaction's EVM gas consumption is the binding constraint rather than native resource consumption. This is a realistic scenario: EVM operations with high gas costs but low proving overhead (e.g., cold `SLOAD` at 2100 gas, or large calldata token costs) can produce a `gas_used` that exceeds `native_used / native_per_gas`. Any unprivileged sender submitting a standard EVM transaction can trigger this path without any special setup. The condition is more likely when `native_per_gas` is high (high `gas_price` or low `native_price`), which is the normal operating range for a busy network.

---

### Recommendation

Inside the `#[cfg(not(feature = "unlimited_native"))]` block, add the symmetric downward adjustment:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else if delta_gas < 0 {
    // Native consumption was less than gas consumption implies;
    // reduce gas_used so the user is refunded the difference.
    gas_used = gas_used.saturating_sub((-delta_gas) as u64);
    // Respect the minimal_gas_used floor already applied above.
    gas_used = gas_used.max(minimal_gas_used);
}
```

This mirrors the existing upward adjustment and ensures the user's refund correctly reflects actual resource consumption in both directions.

---

### Proof of Concept

**Setup:**
- `gas_price = 1000`, `native_price = 100` → `native_per_gas = 10`
- `gas_limit = 100_000`
- Transaction executes and consumes `gas_used_evm = 50_000` EVM gas
- Native resource consumed: `native_used = 400_000`

**Calculation:**
```
delta_gas = (400_000 / 10) − 50_000 = 40_000 − 50_000 = −10_000
```

Because `delta_gas < 0`, the code takes no action. `gas_used` remains `50_000`.

**Actual refund:**
```
gas_price × (gas_limit − gas_used) = 1_000 × 50_000 = 50_000_000 tokens
```

**Correct refund (if delta_gas were applied):**
```
gas_price × (gas_limit − 40_000) = 1_000 × 60_000 = 60_000_000 tokens
```

**User loss: `10_000_000` base tokens**, silently transferred to the operator.

The entry path is a plain EVM transaction submitted by any unprivileged sender. No governance, oracle manipulation, or privileged access is required.

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L277-334)
```rust
    let pay_to_operator = U256::from(gas_used)
        .checked_mul(U256::from(gas_price))
        .ok_or(internal_error!("gu*gp"))?;
    // Use FORMAL_INFINITE for post-execution operations (coinbase transfer,
    // asset tracker notifications, refund transfer, log emission).
    // These cannot fail due to resource exhaustion. Their native cost is
    // accounted for as intrinsic and is not included in
    // computational_native_used (native_used only reflects native for
    // pubdata + native used for charged computation).
    let mut inf_resources = S::Resources::FORMAL_INFINITE;

    let coinbase = system.get_coinbase();
    // Mint operator fee portion of the deposit to coinbase.
    mint_base_token::<S, Config>(
        system,
        system_functions,
        memories.reborrow(),
        &pay_to_operator,
        &coinbase,
        l1_chain_id,
        &mut inf_resources,
        tracer,
        validator,
    )
    .map_err(|e| match e.root_cause() {
        RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
            internal_error!("Out of ergs on infinite ergs").into()
        }
        RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => {
            internal_error!("Out of native on infinite").into()
        }
        _ => e,
    })?;

    // Refund
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
```
