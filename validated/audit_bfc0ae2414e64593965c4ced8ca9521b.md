### Title
Missing Intrinsic Gas Floor in ZK Transaction Flow Allows EVM Refunds to Undercut Operator Compensation - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

In ZKsync OS's ZK transaction flow, `compute_calldata_tokens` returns `minimal_gas_used = 0` when the `eip_7623` feature is disabled. This zero floor propagates into `compute_gas_refund`, where it is supposed to prevent EVM refunds from reducing the operator's payment below the 21,000-gas intrinsic cost. The Ethereum transaction flow correctly uses `TX_INTRINSIC_GAS = 21_000` as this floor. The ZK flow omits it, allowing EVM SSTORE-clearing refunds to reduce the operator's effective gas payment below the intrinsic cost — a direct resource accounting analog to M-16.

---

### Finding Description

**Root cause — `compute_calldata_tokens` in the ZK flow:** [1](#0-0) 

When `eip_7623` is not compiled in, the function returns `(num_tokens, 0)` — the second element is `minimal_gas_used`. The doc comment even states: *"floor gas == 0, if EIP-7623 disabled."*

This value is stored as `minimal_ergs_to_charge`: [2](#0-1) 

And later used as the floor in `compute_gas_refund`: [3](#0-2) [4](#0-3) 

With `minimal_gas_used = 0`, the `max(gas_used, minimal_gas_used)` floor does nothing.

**Contrast with the Ethereum flow:** [5](#0-4) 

The Ethereum flow always returns `TX_INTRINSIC_GAS = 21_000` as `minimal_gas_used` when EIP-7623 is off, which is then passed as the floor into `compute_gas_refund` via `minimal_gas_to_charge`. [6](#0-5) 

**How EVM refunds can undercut the intrinsic gas:**

`compute_gas_refund` computes `gas_used` as `gas_limit − remaining_ergs / ERGS_PER_GAS`. Because `create_resources_for_tx` deducts `intrinsic_gas` before execution, the minimum `gas_used` before refunds is `intrinsic_gas + X` (where X ≥ 0 is body gas). EVM refunds are then applied: [7](#0-6) 

Refunds are capped at `gas_used / 5`. For a transaction that does one warm SSTORE clear (X ≈ 3,000 gas):

- `gas_used` before refund = 21,000 + 3,000 = 24,000
- `max_refund` = 24,000 / 5 = 4,800
- `evm_refund` = min(15,000, 4,800) = 4,800
- `gas_used` after refund = **19,200 < 21,000**

The operator is paid `19,200 × gas_price` instead of at least `21,000 × gas_price`. The user receives an extra `1,800 × gas_price` refund they are not entitled to.

The general condition for `gas_used_after_refund < intrinsic_gas` is `X < 5,250` gas with maximum refunds — achievable with any warm SSTORE-clearing operation.

---

### Impact Explanation

The operator (sequencer/coinbase) receives less than the 21,000-gas intrinsic cost for any L2 ZK-flow transaction that clears storage slots. The user receives a correspondingly inflated refund. The maximum per-transaction loss to the operator is bounded by `intrinsic_gas / 5 = 4,200 gas units × gas_price`. At scale (many transactions per block), this represents a systematic under-payment to the operator and over-refund to users — a resource accounting bug with direct economic impact on operator revenue.

---

### Likelihood Explanation

SSTORE-clearing operations (setting a storage slot to zero) are extremely common in DeFi: token approvals being reset, position closures, mapping deletions. Any such transaction with a small body gas footprint (< 5,250 gas beyond intrinsic) triggers the condition. No special attacker capability is required — any unprivileged transaction sender submitting a normal EVM transaction can trigger this path.

---

### Recommendation

Change `compute_calldata_tokens` in the ZK flow to return `TX_INTRINSIC_GAS` as the floor when EIP-7623 is disabled, matching the Ethereum flow:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs
#[cfg(not(feature = "eip_7623"))]
{
-   (num_tokens, 0)
+   (num_tokens, TX_INTRINSIC_GAS)
}
```

This ensures `minimal_ergs_to_charge` is always at least `TX_INTRINSIC_GAS × ERGS_PER_GAS`, protecting the intrinsic gas from being eroded by EVM refunds — consistent with standard EVM semantics and the existing Ethereum flow.

---

### Proof of Concept

1. Submit an L2 ZK-flow transaction (EIP-7623 disabled) that:
   - Has a gas limit of, say, 30,000
   - Calls a contract that performs one warm SSTORE clear (sets a previously non-zero slot to zero)
   - Body gas used X ≈ 3,000

2. Trace through `compute_gas_refund`:
   - `gas_used` before refund = 30,000 − (30,000 − 21,000 − 3,000) = 24,000
   - `evm_refund` = min(15,000, 24,000/5) = 4,800
   - `gas_used` after `max(gas_used, 0)` = 24,000 − 4,800 = **19,200**

3. Operator receives `19,200 × gas_price`; user receives `(30,000 − 19,200) × gas_price = 10,800 × gas_price` refund.

4. With the Ethereum flow's floor of 21,000, `gas_used` would be `max(19,200, 21,000) = 21,000`, and the operator would receive `21,000 × gas_price`.

The discrepancy — 1,800 gas units per transaction — flows from the missing `TX_INTRINSIC_GAS` floor in `compute_calldata_tokens` for the ZK path. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L497-497)
```rust
        minimal_ergs_to_charge: Ergs(minimal_gas_used.saturating_mul(ERGS_PER_GAS)),
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L519-542)
```rust
#[allow(unused_variables)]
pub(crate) fn compute_calldata_tokens<S: SystemTypes>(
    system: &mut System<S>,
    calldata: &[u8],
) -> (u64, u64) {
    let zero_bytes = calldata.iter().filter(|byte| **byte == 0).count() as u64;
    let non_zero_bytes = (calldata.len() as u64) - zero_bytes;
    let zero_bytes_factor = zero_bytes.saturating_mul(CALLDATA_ZERO_BYTE_TOKEN_FACTOR);
    let non_zero_bytes_factor = non_zero_bytes.saturating_mul(CALLDATA_NON_ZERO_BYTE_TOKEN_FACTOR);
    let num_tokens = zero_bytes_factor.saturating_add(non_zero_bytes_factor);

    #[cfg(feature = "eip_7623")]
    {
        let floor_tokens_gas_cost = num_tokens.saturating_mul(TOTAL_COST_FLOOR_PER_TOKEN);
        let intrinsic_gas = TX_INTRINSIC_GAS.saturating_add(floor_tokens_gas_cost);

        (num_tokens, intrinsic_gas)
    }

    #[cfg(not(feature = "eip_7623"))]
    {
        (num_tokens, 0)
    }
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

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L38-56)
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
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs (L159-163)
```rust
        #[cfg(not(feature = "eip_7623"))]
        {
            (num_tokens, TX_INTRINSIC_GAS)
        }
    };
```

**File:** basic_bootloader/src/bootloader/constants.rs (L41-42)
```rust
/// Transaction intrinsic gas cost.
pub const TX_INTRINSIC_GAS: u64 = 21_000;
```
