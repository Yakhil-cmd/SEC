### Title
EIP-3529 Gas Refund Negated by Wrong Base in `delta_gas` Calculation — (`basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

### Summary

In `compute_gas_refund`, the ZKsync-specific `delta_gas` adjustment for native resource consumption is computed against the **post-EIP-3529-refund** `gas_used` instead of the **pre-refund** `gas_used`. This causes the EIP-3529 storage-clearing refund to be completely negated whenever `delta_gas > 0`, meaning users who perform SSTORE operations that clear storage slots (generating EIP-3529 refunds) and also consume significant native resources receive no benefit from those refunds and overpay fees to the operator.

### Finding Description

`compute_gas_refund` in `refund_calculation.rs` executes the following sequence:

1. **Compute `gas_used` from remaining ergs** (line 31–33)
2. **Apply EIP-3529 refund**: `gas_used -= evm_refund` (line 48)
3. **Apply EIP-7623 floor**: `gas_used = max(gas_used, minimal_gas_used)` (line 56)
4. **Compute `delta_gas`** against this post-refund, post-floor `gas_used` (line 72):
   ```rust
   let delta_gas = (native_used / native_per_gas) as i64 - (gas_used as i64);
   ```
5. **If `delta_gas > 0`**, add it back: `gas_used += delta_gas` (line 78) [1](#0-0) 

When `delta_gas > 0`, the algebra collapses to:

```
gas_used_final
  = gas_used_post_floor + delta_gas
  = gas_used_post_floor + (native_used/native_per_gas − gas_used_post_floor)
  = native_used / native_per_gas
```

The final `gas_used` equals `native_used / native_per_gas` **regardless of the EIP-3529 refund**. The refund is arithmetically cancelled out.

The correct base for `delta_gas` should be the **pre-refund** `gas_used` (the actual EVM gas consumed before applying the storage-clearing discount). Using the post-refund value inflates `delta_gas` by exactly `evm_refund`, which then gets added back to `gas_used`, erasing the refund entirely.

The `minimal_ergs_to_charge` (EIP-7623 floor) stored in `TxContextForPreAndPostProcessing` is converted to gas and passed as `min_gas_used` into this function, compounding the wrong-base issue when the floor is also active. [2](#0-1) 

The `TODO: return delta_gas to gas_used?` comment at line 80 confirms the developers were uncertain about the sign-handling of this adjustment, suggesting the interaction with EIP-3529 was not fully reasoned through. [3](#0-2) 

### Impact Explanation

- Users who perform SSTORE operations that clear storage (generating EIP-3529 refunds of up to 4,800 gas per slot) **and** whose transaction has `delta_gas > 0` (native resource consumption exceeds EVM gas consumption) receive **zero benefit** from those refunds.
- The operator is paid `gas_used * gas_price` where `gas_used` is inflated by the lost refund — the operator collects fees the user should have been refunded.
- Maximum overcharge per transaction: up to 20% of `gas_used` (the EIP-3529 cap of 1/5), multiplied by `gas_price`. For a 1 M gas transaction at 1 gwei, this is up to 200,000 gwei = 0.0002 ETH per transaction.
- This is a direct resource accounting bug: the wrong quantity is used as the base for a percentage-style adjustment, exactly analogous to the external report's liquidation bonus being applied to `(collateral − debt)` instead of `debt`.

### Likelihood Explanation

`delta_gas > 0` requires `native_used / native_per_gas > gas_used`, i.e., native resource consumption (proving cost) exceeds EVM gas consumption in gas-equivalent units. This is a common condition for ZKsync transactions because:

- `native_per_gas = gas_price / native_price` — at low gas prices (base fee only, no priority fee), `native_per_gas` is small, making `native_used / native_per_gas` large.
- Complex transactions (many storage reads, keccak calls, precompile invocations) consume significant native resources.

DeFi transactions that both clear storage (e.g., deleting allowances, settling positions) and perform complex computation are a realistic and common trigger. The attacker-controlled entry path is simply submitting such a transaction; no privileged access is required.

### Recommendation

Capture `gas_used` **before** applying the EIP-3529 refund and use that pre-refund value as the base for `delta_gas`:

```rust
let gas_used_pre_refund = gas_used; // save before EIP-3529 refund

let evm_refund = { ... };
gas_used -= evm_refund;
let mut gas_used = core::cmp::max(gas_used, minimal_gas_used);

// Use pre-refund gas_used as the base so delta_gas does not negate the EIP-3529 refund
let delta_gas = (native_used / native_per_gas) as i64 - (gas_used_pre_refund as i64);
if delta_gas > 0 {
    gas_used += delta_gas as u64;
}
```

This preserves the EIP-3529 refund benefit while still ensuring the user pays for native resource consumption.

### Proof of Concept

**Setup**: `gas_limit = 200_000`, `gas_price = 1`, `native_price = 1`, so `native_per_gas = 1`.

- Transaction executes and consumes 100,000 ergs-worth of EVM gas → `gas_used_raw = 100_000`.
- Transaction clears 5 storage slots → `evm_refund = min(5 * 4800, 100_000/5) = min(24_000, 20_000) = 20_000`.
- After refund: `gas_used = 80_000`.
- Native resources consumed: 120,000 native units → `native_used = 120_000`.
- `delta_gas = (120_000 / 1) − 80_000 = 40_000 > 0`.
- `gas_used += 40_000` → `gas_used_final = 120_000`.

**Expected** (correct): `gas_used_final` should reflect the EIP-3529 refund. Using pre-refund base: `delta_gas = 120_000 − 100_000 = 20_000`; `gas_used_final = 80_000 + 20_000 = 100_000`. The user benefits from the 20,000-gas refund.

**Actual** (buggy): `gas_used_final = 120_000`. The 20,000-gas EIP-3529 refund is completely lost. The operator receives `120_000 * gas_price` instead of `100_000 * gas_price`. [4](#0-3) [5](#0-4)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L38-81)
```rust
    // Following EIP-3529, refunds are capped to 1/5 of the gas used
    let evm_refund = {
        let full_refund_ergs = system.io.get_refund_counter().ergs();
        let full_refund_gas = full_refund_ergs.0.div_floor(ERGS_PER_GAS);
        let max_refund = gas_used / 5;
        core::cmp::min(full_refund_gas, max_refund)
    };

    system_log!(system, "Gas refund from refund counters = {evm_refund}\n");

    gas_used -= evm_refund;

    system_log!(
        system,
        "Minimal gas used from validation = {minimal_gas_used}\n"
    );

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L426-434)
```rust
        let min_gas_used = context.minimal_ergs_to_charge.0 / ERGS_PER_GAS;
        let refund_info = compute_gas_refund(
            system,
            to_charge_for_pubdata,
            transaction.gas_limit(),
            min_gas_used,
            context.native_per_gas,
            &mut context.resources.main_resources,
        )?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L493-497)
```rust
    Ok(TxContextForPreAndPostProcessing {
        resources: tx_resources,
        fee_to_prepay,
        gas_price,
        minimal_ergs_to_charge: Ergs(minimal_gas_used.saturating_mul(ERGS_PER_GAS)),
```
