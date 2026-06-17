### Title
Operator-Controlled `native_price` Can Force Users to Overpay Gas Due to Asymmetric `deltaGas` Adjustment - (File: `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

In ZKsync OS, the operator sets `native_price` per block, which directly controls the `nativePerGas` ratio used to derive the user's native resource limit. When `deltaGas` (the difference between native-implied gas and EVM gas used) is **negative** — meaning the user consumed more EVM gas than native resources warranted — the system silently discards the negative delta and does **not** reduce `gas_used`. This is an acknowledged asymmetry (marked with a `TODO` comment in the code), but it means users are systematically overcharged for gas in scenarios where the operator sets a high `native_price` relative to actual proving cost, causing `nativePerGas` to be low and `deltaGas` to be negative.

---

### Finding Description

The double resource accounting model in ZKsync OS derives a native resource limit from the transaction's gas price and the operator-supplied `native_price`:

```
nativePerGas = gasPrice / native_price
nativeLimit  = gasLimit * nativePerGas
```

After execution, `compute_gas_refund` in `refund_calculation.rs` computes `deltaGas`:

```rust
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
```

The `TODO` comment explicitly acknowledges the missing branch: when `delta_gas < 0` (native consumption was *less* than EVM gas consumption implied), `gas_used` is **not reduced**. The user is charged for the full EVM gas used even though the native resource cost was lower.

The `native_price` is an operator-controlled block-level parameter:

```rust
let native_price = system.get_native_price();
// ...
u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
    InvalidTransaction::NativeResourcesAreTooExpensive,
))?
```

An operator can set `native_price` to a high value, making `nativePerGas` small, so the user's native limit is tight. If the transaction's EVM execution is gas-heavy but native-light (e.g., many cheap EVM opcodes that are trivial to prove), `native_used / native_per_gas` will be much less than `gas_used`, producing a large negative `delta_gas`. The user is charged the full EVM `gas_used` with no reduction.

The analog to the Atlas report is direct: just as the Atlas bundler controls `tx.gasprice` and can force solvers to overpay, the ZKsync OS operator controls `native_price` and can force users to overpay EVM gas fees by manipulating the `nativePerGas` ratio such that the negative `deltaGas` correction is never applied.

---

### Impact Explanation

Users are overcharged for gas on every transaction where `delta_gas < 0`. The overcharge is:

```
overcharge = |delta_gas| * gas_price
           = (gas_used - native_used / native_per_gas) * gas_price
```

This is a direct financial loss to users. The operator receives the excess fee (via `token_to_pay_operator = gas_used * gas_price_for_operator`). The higher the operator sets `native_price`, the smaller `nativePerGas` becomes, and the larger the potential overcharge per transaction.

**Scope match**: This is a resource accounting bug causing direct loss of user funds (overpayment of fees) triggered by an operator-controlled parameter. It falls within the ZKsync OS Immunefi scope as a state-transition correctness issue affecting fee accounting.

---

### Likelihood Explanation

- The `native_price` is set per block by the operator and is not constrained by any on-chain bound visible to users at signing time.
- The `TODO` comment at line 80 of `refund_calculation.rs` confirms the developers are aware the negative branch is unimplemented.
- Any transaction where EVM gas consumption exceeds `native_used / native_per_gas` triggers this — which is common for EVM-heavy, proving-light workloads (e.g., many SLOAD/SSTORE with warm slots, arithmetic-heavy contracts).
- The effect is amplified when the operator raises `native_price` (lowering `nativePerGas`), which is a routine operational parameter.

---

### Recommendation

In `compute_gas_refund`, handle the negative `delta_gas` case symmetrically:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else if delta_gas < 0 {
    // Native consumption was less than EVM gas implied.
    // Reduce gas_used, but not below minimal_gas_used.
    let reduction = (-delta_gas) as u64;
    gas_used = gas_used.saturating_sub(reduction).max(minimal_gas_used);
}
```

This ensures users are not overcharged when their transaction's native resource consumption is lower than the EVM gas consumed. The `minimal_gas_used` floor prevents the refund from exceeding the intrinsic cost floor.

---

### Proof of Concept

1. Operator sets `native_price = 10_000` (high value → small `nativePerGas`).
2. User submits EIP-1559 tx with `max_fee_per_gas = 100_000`, `gas_limit = 200_000`.
   - `nativePerGas = ceil(100_000 / 10_000) = 10`
   - `nativeLimit = 200_000 * 10 = 2_000_000`
3. Transaction executes: EVM uses `gas_used = 150_000` ergs-equivalent; native uses only `native_used = 500_000` (proving is cheap for this workload).
4. In `compute_gas_refund`:
   - `delta_gas = (500_000 / 10) as i64 - 150_000 as i64 = 50_000 - 150_000 = -100_000`
   - Since `delta_gas < 0`, the branch is skipped.
   - `gas_used` remains `150_000` instead of being reduced to `50_000`.
5. User is charged for `150_000` gas instead of `50_000` gas — a 3× overcharge.
6. The operator receives `150_000 * gas_price` instead of `50_000 * gas_price`.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L121-139)
```rust
    let native_per_gas = {
        if native_price.is_zero() {
            return Err(internal_error!("Native price cannot be 0").into());
        }

        if cfg!(feature = "resources_for_tester") {
            crate::bootloader::constants::TESTER_NATIVE_PER_GAS
        } else if Config::SIMULATION && gas_price.is_zero() {
            // For simulation, if gas price isn't set, we use base fee
            // for native calculation
            u256_try_to_u64(&system.get_eip1559_basefee().div_ceil(native_price)).ok_or(
                TxError::Validation(InvalidTransaction::NativeResourcesAreTooExpensive),
            )?
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L514-516)
```rust
        let token_to_pay_operator = U256::from(context.gas_used)
            .checked_mul(gas_price_for_operator)
            .ok_or(internal_error!("gu*gpfo"))?;
```
