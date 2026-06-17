### Title
Silent Saturation Chain in L1 Transaction Native Resource Calculation Causes Incorrect Resource Accounting - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

In `prepare_and_check_resources` for L1→L2 transactions, when `gas_price` causes `native_per_gas` to overflow `u64`, the `L1ResourcesPolicy` silently saturates to `u64::MAX` and continues execution. This triggers a chain of subsequent saturations and underflows — each silently swallowed by `L1ResourcesPolicy::handle_arithmetic_error` returning `Ok(0)` — analogous to the Yarn `;`-chained command groups where a failed group does not halt subsequent groups. The combined effect is that the transaction proceeds with `native_limit = 0`, causing it to be immediately reverted due to out-of-native resources while the user loses their entire gas fee, and the reported `native_used` value becomes `u64::MAX`, which is incorrect.

---

### Finding Description

`prepare_and_check_resources` in `process_l1_transaction.rs` uses `L1ResourcesPolicy` to handle arithmetic errors by logging and saturating instead of failing, because L1 transactions cannot be invalidated (doing so would halt the chain via the priority queue). [1](#0-0) 

When `gas_price > u64::MAX * L1_TX_NATIVE_PRICE` (where `L1_TX_NATIVE_PRICE = 10`):

**Step 1** — `native_per_gas` overflows, silently saturates to `u64::MAX`:
```
native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price)).unwrap_or_else(|| u64::MAX)
```

**Step 2** — `native_per_pubdata` overflows (if `gas_per_pubdata > 0`), silently saturates to `u64::MAX`:
```
native_per_pubdata = (gas_per_pubdata as u64).checked_mul(native_per_gas).unwrap_or_else(|| u64::MAX)
```

**Step 3** — `native_prepaid_from_gas` overflows, silently saturates to `u64::MAX`:
```
native_prepaid_from_gas = native_per_gas.checked_mul(gas_limit).unwrap_or_else(|| u64::MAX)
``` [2](#0-1) 

**Step 4** — Inside `create_resources_for_tx`, the intrinsic pubdata overhead calculation saturates to `u64::MAX`, causing `native_limit` to underflow to 0. `L1ResourcesPolicy::handle_arithmetic_error` silently returns `Ok(0)`: [3](#0-2) 

**Step 5** — The subsequent intrinsic computational native subtraction also underflows. `L1ResourcesPolicy` again silently returns `Ok(0)`: [4](#0-3) 

The `L1ResourcesPolicy` error handler that enables this silent continuation: [5](#0-4) 

The transaction is then created with `native_limit = 0` and `free_native = false` (since `native_per_gas == 0` is `false` when saturated to `u64::MAX`). Any computation immediately exhausts native resources.

Additionally, in `compute_gas_refund`, the `full_native_limit` is computed as:
```
gas_limit.saturating_mul(native_per_gas)  // = gas_limit * u64::MAX = u64::MAX
```
and `native_used = u64::MAX - resources.native().remaining() ≈ u64::MAX`, which is reported as an incorrect value in `ZkTxResult`. [6](#0-5) 

---

### Impact Explanation

1. **Incorrect native resource allocation**: The user paid for a large native limit (proportional to `gas_price * gas_limit`) but receives `native_limit = 0`. The transaction is immediately reverted due to out-of-native resources.
2. **Full gas fee loss**: Since all ergs are exhausted (`resources.exhaust_ergs()` is called on out-of-native), `gas_used = gas_limit`, and the user loses their entire prepaid fee.
3. **Incorrect `native_used` reporting**: When `gas_per_pubdata = 0` (allowing the transaction to succeed), `native_used` is reported as `u64::MAX` in `ZkTxResult`, which is incorrect and could cause forward/proving divergence if the proving system independently computes native consumption. [7](#0-6) 

---

### Likelihood Explanation

**Low.** Triggering the overflow requires `gas_price > u64::MAX * 10 ≈ 1.84 × 10²⁰ wei` per gas unit. The code itself acknowledges this is practically unreachable because L1 contracts validate these parameters: [8](#0-7) 

However, the code explicitly implements this as a fallback for when L1 contracts are not properly updated, and the saturation chain is a real code path that executes silently without any fatal error.

---

### Recommendation

**Short term:** In `prepare_and_check_resources`, after each saturation, check whether the resulting value is `u64::MAX` and, if so, treat the transaction as having zero available gas (immediately revert with exhausted ergs) rather than propagating the saturated value into downstream calculations. This prevents the silent chain of incorrect computations.

**Long term:** Decompose the multi-step resource calculation in `prepare_and_check_resources` into individually validated steps, each with an explicit outcome check before proceeding to the next step. This mirrors the Yarn recommendation: expand chained operations into individually verifiable named steps so that a failure in one step does not silently corrupt subsequent steps.

---

### Proof of Concept

1. Submit an L1→L2 priority transaction with:
   - `gas_price = u128::from(u64::MAX) * 11` (causes `native_per_gas` overflow)
   - `gas_per_pubdata = 1000` (causes `native_per_pubdata` overflow)
   - `gas_limit = 100_000`
   - `total_deposited >= gas_price * gas_limit`

2. In `prepare_and_check_resources`:
   - `native_per_gas = u64::MAX` (saturated, logged)
   - `native_per_pubdata = u64::MAX` (saturated, logged)
   - `native_prepaid_from_gas = u64::MAX` (saturated, logged)

3. In `create_resources_for_tx` with `L1ResourcesPolicy`:
   - `intrinsic_pubdata_overhead = u64::MAX` (saturated)
   - `native_limit = u64::MAX - u64::MAX = 0` → underflow → `Ok(0)`
   - `native_limit = 0 - intrinsic_computational_native` → underflow → `Ok(0)`
   - Transaction created with `native_limit = 0`

4. Transaction body immediately runs out of native resources → reverted.

5. `gas_used = gas_limit`, user loses full fee.

The existing test `test_l1_tx_gas_price_overflow_native_per_gas` confirms the saturation path is reachable and the transaction is processed (not halted): [9](#0-8)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L217-234)
```rust
                Err(e) => {
                    match e.root_cause() {
                        // Out of native / memory is converted to a top-level
                        // revert so post-execution L1 accounting can still run.
                        RootCause::Runtime(runtime @ RuntimeError::FatalRuntimeError(_)) => {
                            system_log!(
                                system,
                                "L1 transaction ran out of native resources or memory {runtime:?}\n"
                            );
                            resources.exhaust_ergs();
                            system.finish_global_frame(Some(&rollback_handle))?;
                            (
                                false,
                                Vec::new_in(system.get_allocator()),
                                None,
                                S::Resources::empty(),
                                memories,
                            )
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L422-433)
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
/// log if this situation ever happens.
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L455-496)
```rust
    let native_per_gas = if is_priority_op {
        if gas_price.is_zero() {
            if Config::SIMULATION {
                u256_try_to_u64(&system.get_eip1559_basefee().div_ceil(native_price))
                    .unwrap_or_else(|| {
                        system_log!(
                            system,
                            "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                        u64::MAX
                    })
            } else {
                FREE_L1_TX_NATIVE_PER_GAS
            }
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).unwrap_or_else(|| {
                system_log!(
                    system,
                    "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
            })
        }
    } else {
        // Upgrade txs are paid by the protocol, so we use a fixed native per gas
        FREE_L1_TX_NATIVE_PER_GAS
    };

    let native_per_pubdata = (gas_per_pubdata as u64)
        .checked_mul(native_per_gas)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native per pubdata calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });

    let native_prepaid_from_gas = native_per_gas.checked_mul(gas_limit)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native prepaid from gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L99-125)
```rust
    fn handle_arithmetic_error(
        system: &mut System<S>,
        error: Self::ArithmeticError,
    ) -> Result<u64, Self::Error> {
        match error {
            L1ArithmeticError::NativeUnderflow { operation } => {
                system_log!(
                    system,
                    "Native underflow during {}, saturating to 0 for L1 tx",
                    operation
                );
                Ok(0)
            }
            L1ArithmeticError::IntrinsicGasOverflow {
                intrinsic_overhead,
                gas_limit,
            } => {
                system_log!(
                    system,
                    "Gas limit {} < intrinsic gas {} for L1 tx, saturating to 0",
                    gas_limit,
                    intrinsic_overhead
                );
                Ok(0)
            }
        }
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L351-359)
```rust
    // Charge intrinsic pubdata
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
        Some(val) => val,
        None => P::handle_arithmetic_error(
            system,
            P::native_underflow_error("subtracting pubdata overhead"),
        )?,
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L382-389)
```rust
    // Charge intrinsic computational native
    let native_limit = match native_limit.checked_sub(intrinsic_computational_native) {
        Some(val) => val,
        None => P::handle_arithmetic_error(
            system,
            P::native_underflow_error("subtracting intrinsic computational native"),
        )?,
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L59-64)
```rust
    let full_native_limit = if cfg!(feature = "unlimited_native") || native_per_gas == 0 {
        u64::MAX - 1
    } else {
        gas_limit.saturating_mul(native_per_gas)
    };
    let native_used = full_native_limit.saturating_sub(resources.native().remaining().as_u64());
```

**File:** tests/instances/transactions/src/l1_tx_resilience.rs (L79-117)
```rust
#[test]
fn test_l1_tx_gas_price_overflow_native_per_gas() {
    let from = address!("1234000000000000000000000000000000000000");
    let to = common_target_address();

    // L1_TX_NATIVE_PRICE = 10
    // To overflow u64 in native_per_gas calculation: gas_price / 10 > u64::MAX
    // So gas_price > u64::MAX * 10
    let overflow_gas_price = u128::from(u64::MAX) * 11;

    let tx = L1TxBuilder::new()
        .from(from)
        .to(to)
        .gas_price(overflow_gas_price)
        .gas_limit(100_000)
        .value(alloy::primitives::U256::from(100))
        .build()
        .into();

    let mut tester =
        TestingFramework::new().with_balance(from, U256::from(1_000_000_000_000_000_u64));

    // The block should complete without panicking (no internal error)
    let result = tester.execute_block_no_panic(vec![tx]);
    assert!(
        result.is_ok(),
        "Block should complete without internal error, got: {:?}",
        result.err()
    );

    // The transaction should be processed (L1 txs cannot be invalidated)
    let output = result.unwrap();
    let tx_result = output.tx_results.first().expect("Should have tx result");
    assert!(
        tx_result.is_ok(),
        "L1 tx should be processed (not rejected with validation error), got: {:?}",
        tx_result
    );
}
```
