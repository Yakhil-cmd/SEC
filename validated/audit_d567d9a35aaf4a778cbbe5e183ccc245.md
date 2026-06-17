### Title
Unchecked Integer Addition in L1 Transaction `computational_native_used` Calculation Bypasses Block Native Limit - (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The `process_l1_transaction` function computes `computational_native_used` using a plain Rust `+` operator, which wraps on overflow in release mode and panics in debug/proving mode. The equivalent L2 transaction path uses `.saturating_add()` for the same computation. Because L1 transactions cannot be rejected (doing so would halt the chain), a crafted L1 priority transaction can trigger this path. The resulting wrapped (underflowed) value is then fed into the block-level accumulator via unchecked additions, potentially allowing the block's native computational limit to be bypassed.

---

### Finding Description

**Root cause 1 — L1 `computational_native_used` unchecked addition:**

In `process_l1_transaction.rs` at line 375–379, the final `computational_native_used` is assembled with a bare `+`:

```rust
let computational_native_used = resources_before_refund
    .diff(initial_resources)
    .native()
    .as_u64()
    + intrinsic_computational_native_charged;   // ← plain `+`, no overflow guard
``` [1](#0-0) 

The equivalent L2 path in `zk/mod.rs` explicitly uses `.saturating_add()`:

```rust
let computational_native_used = context
    .resources_before_refund
    .clone()
    .diff(context.initial_resources.clone())
    .native()
    .as_u64()
    .saturating_add(context.resources.intrinsic_computational_native_charged);
``` [2](#0-1) 

**Root cause 2 — Block accumulator unchecked additions:**

In `tx_loop.rs` at lines 139–148, all four block-level resource accumulators are updated with bare `+`:

```rust
let next_block_gas_used =
    block_data.block_gas_used + tx_processing_result.gas_used;
let next_block_computational_native_used = block_data
    .block_computational_native_used
    + tx_processing_result.computational_native_used;
let next_block_pubdata_used =
    block_data.block_pubdata_used + tx_processing_result.pubdata_used;
let next_block_blob_gas_used =
    block_data.block_blob_gas_used + tx_processing_result.blob_gas_used;
``` [3](#0-2) 

These four values are immediately passed to `check_for_block_limits`, which compares them against `MAX_NATIVE_COMPUTATIONAL` and other block caps:

```rust
} else if !cfg!(feature = "resources_for_tester")
    && computational_native_used > MAX_NATIVE_COMPUTATIONAL
{
``` [4](#0-3) 

If any accumulator wraps to a small value due to overflow, the comparison passes and the transaction is accepted into the block with incorrect resource accounting.

**Why L1 is the critical path:**

L1 transactions are processed by `prepare_and_check_resources`, which uses saturating arithmetic for intermediate calculations (e.g., `native_per_gas`, `native_prepaid_from_gas`) but does not prevent the final `computational_native_used` sum from overflowing. [5](#0-4) 

Crucially, L1 transactions **cannot be invalidated** — the code explicitly states that doing so would halt the chain: [6](#0-5) 

---

### Impact Explanation

**In release/proving mode (RISC-V target):** Rust integer overflow wraps silently. A wrapped `computational_native_used` value (e.g., near zero) is fed into the block accumulator. The block limit check `next_block_computational_native_used > MAX_NATIVE_COMPUTATIONAL` passes with the wrapped small value, allowing the block to accept more transactions than the native limit permits. This is a **resource accounting bug** that corrupts the block's native usage tracking, potentially causing the prover to process a block that exceeds its computational budget, leading to proof failure or an invalid state transition.

**In debug mode:** The bare `+` panics on overflow, causing block processing to halt entirely — a **denial-of-service** against the sequencer/prover.

The `block_computational_native_used` field is a `u64` with no overflow protection: [7](#0-6) 

---

### Likelihood Explanation

**Moderate.** The overflow requires `resources_before_refund.diff(initial_resources).native().as_u64() + intrinsic_computational_native_charged > u64::MAX`. The execution native is capped at `MAX_NATIVE_COMPUTATIONAL` per transaction (enforced in `create_resources_for_tx`): [8](#0-7) 

The intrinsic native is proportional to calldata length. If `MAX_NATIVE_COMPUTATIONAL` is set to a value close to `u64::MAX / 2` (which is possible given the system's design for high TPS), a large-calldata L1 transaction could push the sum past `u64::MAX`. The block accumulator overflow is more reachable: two consecutive transactions each consuming close to `MAX_NATIVE_COMPUTATIONAL` produce a sum of up to `2 * MAX_NATIVE_COMPUTATIONAL - 1`, which overflows if `MAX_NATIVE_COMPUTATIONAL > u64::MAX / 2`. The attacker controls L1 transaction parameters (gas limit, gas price, calldata) and L1 transactions cannot be rejected.

---

### Recommendation

1. Replace the bare `+` in `process_l1_transaction.rs` line 379 with `.saturating_add()`, matching the L2 path:

```rust
let computational_native_used = resources_before_refund
    .diff(initial_resources)
    .native()
    .as_u64()
    .saturating_add(intrinsic_computational_native_charged);
```

2. Replace all four bare `+` additions in `tx_loop.rs` lines 139–148 with `.saturating_add()`:

```rust
let next_block_gas_used =
    block_data.block_gas_used.saturating_add(tx_processing_result.gas_used);
let next_block_computational_native_used = block_data
    .block_computational_native_used
    .saturating_add(tx_processing_result.computational_native_used);
let next_block_pubdata_used =
    block_data.block_pubdata_used.saturating_add(tx_processing_result.pubdata_used);
let next_block_blob_gas_used =
    block_data.block_blob_gas_used.saturating_add(tx_processing_result.blob_gas_used);
```

Saturating semantics are safe here: if the sum saturates to `u64::MAX`, the block limit check will immediately reject the transaction, which is the correct behavior.

---

### Proof of Concept

1. Craft an L1 priority transaction with:
   - Very large calldata (maximizing `intrinsic_computational_native_charged`)
   - High gas price (maximizing `native_per_gas` → saturates to `u64::MAX`)
   - High gas limit (maximizing `native_prepaid_from_gas`)

2. The transaction is submitted to the L1 priority queue and cannot be rejected by the sequencer.

3. During `process_l1_transaction`, `resources_before_refund.diff(initial_resources).native().as_u64()` approaches `MAX_NATIVE_COMPUTATIONAL`, and `intrinsic_computational_native_charged` is large due to calldata.

4. The bare `+` at line 379 overflows, producing a small `computational_native_used` (e.g., near 0).

5. In `tx_loop.rs`, `next_block_computational_native_used = block_data.block_computational_native_used + 0` (wrapped value) passes the `> MAX_NATIVE_COMPUTATIONAL` check.

6. The block accumulator is updated with the incorrect small value, and subsequent transactions see an artificially low accumulated native usage, allowing the block to exceed `MAX_NATIVE_COMPUTATIONAL` in actual computation while the accounting shows it is within limits. [1](#0-0) [9](#0-8)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L375-379)
```rust
    let computational_native_used = resources_before_refund
        .diff(initial_resources)
        .native()
        .as_u64()
        + intrinsic_computational_native_charged;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L422-432)
```rust
///
/// Compute and perform some checks on fee/resource parameters.
/// This function handles cases that for L2 transactions would be
/// validation errors, as "invalidating" an L1 transaction can halt
/// the chain (due to the priority queue).
/// Note that the "validation errors" are practically unreachable, as
/// gas_limit, gas_price and gas_per_pubdata are either checked or set
/// by the L1 contracts. We decide to handle these cases as a fallback in
/// case the L1 contracts aren't properly updated to reflect a change in
/// ZKsync OS.
/// The approach is to use saturating arithmetic and emit a system
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L490-496)
```rust
    let native_prepaid_from_gas = native_per_gas.checked_mul(gas_limit)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native prepaid from gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L559-565)
```rust
        let computational_native_used = context
            .resources_before_refund
            .clone()
            .diff(context.initial_resources.clone())
            .native()
            .as_u64()
            .saturating_add(context.resources.intrinsic_computational_native_charged);
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/tx_loop.rs (L139-169)
```rust
                            let next_block_gas_used =
                                block_data.block_gas_used + tx_processing_result.gas_used;
                            let next_block_computational_native_used = block_data
                                .block_computational_native_used
                                + tx_processing_result.computational_native_used;
                            let next_block_pubdata_used =
                                block_data.block_pubdata_used + tx_processing_result.pubdata_used;
                            let block_logs_used = system.io.logs_len();
                            let next_block_blob_gas_used =
                                block_data.block_blob_gas_used + tx_processing_result.blob_gas_used;

                            // Check if the transaction made the block reach any of the limits
                            // for gas, native, pubdata or logs.
                            if let Err(err) = check_for_block_limits(
                                system,
                                next_block_gas_used,
                                next_block_computational_native_used,
                                next_block_pubdata_used,
                                block_logs_used,
                                next_block_blob_gas_used,
                            ) {
                                // Revert to state before transaction
                                system.finish_global_frame(Some(&pre_tx_rollback_handle))?;
                                result_keeper.tx_processed(Err(err));
                            } else {
                                // Now update the accumulators
                                block_data.block_gas_used = next_block_gas_used;
                                block_data.block_computational_native_used =
                                    next_block_computational_native_used;
                                block_data.block_pubdata_used = next_block_pubdata_used;
                                block_data.block_blob_gas_used = next_block_blob_gas_used;
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L68-70)
```rust
    } else if !cfg!(feature = "resources_for_tester")
        && computational_native_used > MAX_NATIVE_COMPUTATIONAL
    {
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/block_data.rs (L21-23)
```rust
    pub block_computational_native_used: u64,
    /// Amount of blob gas used in the block
    pub block_blob_gas_used: u64,
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L368-380)
```rust
    let (native_limit, withheld) = if native_limit <= MAX_NATIVE_COMPUTATIONAL {
        (native_limit, S::Resources::from_ergs(Ergs::empty()))
    } else {
        let withheld =
            <<S as zk_ee::system::SystemTypes>::Resources as Resources>::Native::from_computational(
                native_limit - MAX_NATIVE_COMPUTATIONAL,
            );

        (
            MAX_NATIVE_COMPUTATIONAL,
            S::Resources::from_native(withheld),
        )
    };
```
