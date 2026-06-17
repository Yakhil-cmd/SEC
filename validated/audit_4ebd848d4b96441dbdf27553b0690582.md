### Title
Asymmetric `delta_gas` Adjustment in `compute_gas_refund` Silently Routes Excess Gas Fees to Operator Instead of Refunding User — (`File: basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

In `compute_gas_refund`, the `delta_gas` correction that reconciles EVM gas consumption with native resource consumption is applied **only in one direction**: it increases `gas_used` when native consumption exceeds EVM gas consumption, but **never decreases** `gas_used` when native consumption is less. When `delta_gas < 0`, the user is overcharged: the excess fee is silently transferred to the operator rather than refunded to the sender. A TODO comment in the code explicitly acknowledges this gap.

---

### Finding Description

`compute_gas_refund` in `refund_calculation.rs` computes the final `gas_used` value that drives both the user refund and the operator payment. After accounting for pubdata and EVM storage refunds, it computes a `delta_gas` to reconcile the two resource dimensions:

```
delta_gas = (native_used / native_per_gas) - gas_used
```

The adjustment block is:

```rust
if delta_gas > 0 {
    // native consumption > gas consumption → charge more gas
    gas_used += delta_gas as u64;
}
// TODO: return delta_gas to gas_used?
``` [1](#0-0) 

When `delta_gas < 0` (native consumption is *less* than EVM gas consumption), `gas_used` is left unchanged. The downstream accounting in `refund_and_commit_fee` then:

- Refunds the user: `(gas_limit - gas_used) * gas_price` — **less than owed**
- Pays the operator: `gas_used * gas_price` — **more than owed** [2](#0-1) [3](#0-2) 

The same pattern applies in the Ethereum transaction flow: [4](#0-3) [5](#0-4) 

The documentation in `double_resource_accounting.md` only describes the `delta_gas > 0` case and is silent on the negative case, confirming this is not an intentional design choice: [6](#0-5) 

---

### Impact Explanation

Every transaction where `native_used / native_per_gas < gas_used` results in the user being overcharged. The overcharge amount is:

```
overcharge = |delta_gas| * gas_price
           = (gas_used - native_used / native_per_gas) * gas_price
```

This amount is not burned — it is transferred to the operator via the inflated `gas_used * gas_price` payment. The user's balance is permanently reduced by more than the actual cost of the transaction. This is a direct, quantifiable loss of user funds routed to the wrong party (operator instead of user), directly analogous to the OmniXMultisender pattern where excess ETH was locked in the contract instead of returned to the sender.

---

### Likelihood Explanation

The condition `delta_gas < 0` arises whenever:

```
native_used * native_price < gas_used * gas_price
```

This is reachable by any unprivileged user submitting a standard L2 transaction. It occurs when EVM operations consume more gas than their native proving cost implies — for example, transactions heavy in arithmetic or memory operations that are cheap to prove but expensive in EVM gas. The magnitude of the overcharge scales with `gas_price`, so it is most significant during periods of high gas prices. The condition is not rare; it is a normal operating scenario for many transaction types.

---

### Recommendation

Apply the `delta_gas` correction symmetrically. When `delta_gas < 0`, reduce `gas_used` by `|delta_gas|` (subject to the `minimal_gas_used` floor) so that the user is refunded for the unused native-implied gas:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else if delta_gas < 0 {
    // Return unused native-implied gas to the user
    let reduction = (-delta_gas) as u64;
    gas_used = gas_used.saturating_sub(reduction).max(minimal_gas_used);
}
```

This ensures the total fee paid equals `actual_gas_used * gas_price` regardless of which resource dimension is the binding constraint.

---

### Proof of Concept

**Setup:**
- `gas_limit = 100_000`, `gas_price = 1_000`, `native_price = 100`
- `native_per_gas = gas_price / native_price = 10`
- Transaction executes and consumes `gas_used = 50_000` EVM gas
- Native resources consumed: `native_used = 400_000` (less than `50_000 * 10 = 500_000`)

**Current behavior:**
```
delta_gas = (400_000 / 10) - 50_000 = 40_000 - 50_000 = -10_000
→ delta_gas < 0, so gas_used stays at 50_000

User refund  = (100_000 - 50_000) * 1_000 = 50_000_000
Operator pay = 50_000 * 1_000             = 50_000_000
```

**Correct behavior (with symmetric fix):**
```
gas_used should be reduced to 40_000

User refund  = (100_000 - 40_000) * 1_000 = 60_000_000
Operator pay = 40_000 * 1_000             = 40_000_000
```

**User loss per transaction:** `10_000 gas × 1_000 gas_price = 10_000_000 wei`, silently transferred to the operator. Any unprivileged sender submitting a transaction through the ZKsync OS bootloader can trigger this path; no special privileges are required. [1](#0-0)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L514-516)
```rust
        let token_to_pay_operator = U256::from(context.gas_used)
            .checked_mul(gas_price_for_operator)
            .ok_or(internal_error!("gu*gpfo"))?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs (L508-518)
```rust
        if context.tx_gas_limit > context.gas_used {
            system_log!(
                system,
                "Gas price for refund is {:?}\n",
                &context.tx_level_metadata.tx_gas_price
            );

            // refund
            let receiver = transaction.from();
            let refund = context.tx_level_metadata.tx_gas_price
                * U256::from(context.tx_gas_limit - context.gas_used); // can not overflow
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs (L593-593)
```rust
            let fee = context.priority_fee_per_gas * U256::from(context.gas_used); // can not overflow
```

**File:** docs/double_resource_accounting.md (L47-52)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.

Finally, any remaining gas left is refunded as usual.
```
