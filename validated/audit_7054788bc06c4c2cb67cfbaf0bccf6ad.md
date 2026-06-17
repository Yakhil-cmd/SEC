### Title
Silent Gas Refund Suppression When `delta_gas < 0`: User Overpays Fees Without Recourse - (File: `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

In `compute_gas_refund`, the double-resource accounting adjustment (`delta_gas`) is only applied when it is positive (native cost exceeds gas cost). When `delta_gas` is negative — meaning the user's EVM gas consumption was higher than what native resource consumption would imply — the negative delta is silently discarded. The user is charged for the full EVM gas used without receiving the corresponding gas refund that the native-resource accounting would entitle them to. The code even marks this with a `// TODO: return delta_gas to gas_used?` comment, acknowledging the omission.

---

### Finding Description

`compute_gas_refund` in `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs` implements ZKsync OS's dual-resource gas accounting. After computing `gas_used` from ergs and `native_used` from native resource consumption, it calculates:

```rust
let delta_gas = (native_used / native_per_gas) as i64 - (gas_used as i64);

if delta_gas > 0 {
    // native cost > gas cost: charge extra gas
    gas_used += delta_gas as u64;
}
// TODO: return delta_gas to gas_used?
```

The design intent (documented in `docs/double_resource_accounting.md`) is:

> `deltaGas := (nativeUsed / nativePerGas) - gasUsed`
> If `deltaGas > 0`, we add it to `gasUsed`...
> **Finally, any remaining gas left is refunded as usual.**

The documentation only describes the `delta_gas > 0` case. When `delta_gas < 0` — i.e., the transaction consumed more EVM gas than the native resource equivalent — the negative delta is dropped. `gas_used` remains at the higher EVM-gas-derived value, and the user receives a smaller refund than the native resource accounting would justify.

The `// TODO: return delta_gas to gas_used?` comment at line 80 is a developer acknowledgment that this case is unhandled.

The resulting `gas_used` is then used in `refund_and_commit_fee` (both in `zk/mod.rs` and `ethereum/mod.rs`) to compute:
- `token_to_refund = gas_price * (tx_gas_limit - gas_used)` — the user's refund
- `token_to_pay_operator = gas_used * gas_price_for_operator` — the operator's fee

A higher-than-correct `gas_used` means the user receives less refund and the operator receives more fee than is warranted by actual resource consumption.

---

### Impact Explanation

A user who submits a transaction where EVM gas consumption exceeds the native-resource-implied gas equivalent will be overcharged. The excess fee flows to the operator (coinbase) rather than being refunded to the sender. There is no mechanism to track or recover the overcharged amount. The user's only recourse is to contact the operator, analogous to the external report's scenario where users must contact administrators.

The magnitude of overcharge is bounded by `|delta_gas| * gas_price`, which can be non-trivial for transactions with high EVM gas usage but low native resource consumption (e.g., computation-heavy but pubdata-light transactions).

---

### Likelihood Explanation

This is triggered whenever `(native_used / native_per_gas) < gas_used`, which occurs in any transaction where EVM computation is the dominant cost rather than native/proving cost. This is a normal and common transaction profile. The condition is reachable by any unprivileged transaction sender simply by submitting a standard EVM transaction. The `native_per_gas` ratio is operator-controlled, so the frequency and magnitude of the discrepancy varies with operator configuration, but the code path is always active when `native_per_gas > 0` and the `unlimited_native` feature is not enabled.

---

### Recommendation

When `delta_gas < 0`, reduce `gas_used` by `|delta_gas|` (subject to the `minimal_gas_used` floor) so that the user receives the correct refund:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else if delta_gas < 0 {
    // Native cost < gas cost: refund the difference, but not below minimal_gas_used
    let reduction = (-delta_gas) as u64;
    gas_used = gas_used.saturating_sub(reduction).max(minimal_gas_used);
}
```

Remove the `// TODO` comment once addressed.

---

### Proof of Concept

1. Alice submits an EVM transaction with `gas_limit = 100_000`, `gas_price = 1000`, `native_price = 1`, yielding `native_per_gas = 1000`.
2. The transaction uses 80,000 EVM gas (ergs-derived), but only 50,000,000 native units (i.e., `native_used / native_per_gas = 50,000` gas-equivalent).
3. `delta_gas = 50,000 - 80,000 = -30,000` (negative).
4. The `if delta_gas > 0` branch is skipped; `gas_used` stays at 80,000.
5. Alice's refund: `1000 * (100,000 - 80,000) = 20,000,000` tokens.
6. Correct refund should be: `1000 * (100,000 - 50,000) = 50,000,000` tokens.
7. Alice loses `30,000,000` tokens to the operator with no recourse.

The root cause is at: [1](#0-0) 

The downstream fee split that makes this a funds-loss issue: [2](#0-1) 

The design specification that confirms the negative case is unimplemented: [3](#0-2)

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

**File:** docs/double_resource_accounting.md (L47-52)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.

Finally, any remaining gas left is refunded as usual.
```
