### Title
`native_per_pubdata` Truncates to Zero via Floor Division When `pubdata_price < native_price`, Silently Eliminating Pubdata Native-Resource Charges - (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

The `native_per_pubdata` ratio is computed with floor (integer) division. When the operator-set `pubdata_price` is less than `native_price`, the result truncates to zero. This silently removes all native-resource charges for pubdata from every transaction in the block, allowing any unprivileged sender to generate unbounded pubdata at zero native cost — an exact structural analog to the H-07 "multiplier rounds to zero" class.

---

### Finding Description

In `validate_and_compute_fee_for_transaction` (L2 path), the ratio of pubdata cost to native cost is computed as:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs:142-143
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

`wrapping_div` is floor division. When `pubdata_price < native_price` (e.g., `pubdata_price = 5`, `native_price = 10`), the result is `0`. No error is raised; `native_per_pubdata = 0` is silently accepted and propagated.

The same truncation exists in the off-chain helper:

```rust
// api/src/helpers.rs:427
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [2](#0-1) 

Contrast with `native_per_gas`, which correctly uses `div_ceil` to guarantee a non-zero result:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs:135
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [3](#0-2) 

Once `native_per_pubdata = 0` is passed into `create_resources_for_tx`, the intrinsic pubdata overhead is zeroed:

```rust
// basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs:352
let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
``` [4](#0-3) 

And at post-execution pubdata charging, `get_resources_to_charge_for_pubdata` computes:

```rust
let native = current_pubdata_spent.checked_mul(native_per_pubdata)  // = 0
``` [5](#0-4) 

So every storage write, event emission, and L2→L1 message in the transaction costs zero native resources for pubdata.

The default test metadata already demonstrates this condition in practice — `pubdata_price = 0`, `native_price = 10`: [6](#0-5) 

---

### Impact Explanation

`native_per_pubdata = 0` means:

1. **No native resources are reserved for intrinsic pubdata** during transaction setup.
2. **No native resources are charged for execution pubdata** after the transaction body runs.
3. The `delta_gas` correction in `compute_gas_refund` only adjusts for `native_per_gas`, not pubdata; pubdata native cost is entirely invisible to gas accounting. [7](#0-6) 

A transaction that writes to N storage slots generates N × 32 bytes of pubdata. With `native_per_pubdata = 0`, all of that pubdata is proven by the sequencer/prover at zero cost to the sender. The prover bears the full RISC-V cycle cost of proving the pubdata without compensation. This is a **resource accounting bug** with a direct funds-loss path for the protocol: the operator subsidizes unbounded pubdata proving costs.

---

### Likelihood Explanation

The condition `pubdata_price < native_price` is a normal operational state. `pubdata_price` reflects L1 calldata/blob cost and `native_price` reflects RISC-V proving cost per cycle; these are independent operator-set values. Any block where `pubdata_price` (in wei per byte) is numerically smaller than `native_price` (in wei per cycle) triggers the truncation. The default test configuration (`pubdata_price = 0`, `native_price = 10`) confirms this is not an edge case. Any unprivileged L2 transaction sender can exploit this whenever the condition holds, simply by submitting a transaction that writes to storage.

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for `native_per_pubdata`, mirroring the treatment of `native_per_gas`:

```rust
// validation_impl.rs:142
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs:427`. This ensures that even when `pubdata_price` is fractionally smaller than `native_price`, at least 1 native unit is charged per pubdata byte, preserving the invariant that pubdata is never free.

---

### Proof of Concept

1. Operator sets block metadata: `pubdata_price = 5`, `native_price = 10`.
2. `native_per_pubdata = 5.wrapping_div(10) = 0`.
3. Attacker submits an L2 EIP-1559 transaction calling a contract that writes to 100 storage slots (generating ~3200 bytes of pubdata).
4. `create_resources_for_tx` sets `intrinsic_pubdata_overhead = 0 * intrinsic_pubdata = 0` — no native reserved for pubdata.
5. Post-execution: `get_resources_to_charge_for_pubdata` returns `3200 * 0 = 0` native — no native charged for execution pubdata.
6. Transaction succeeds; attacker pays only EVM gas. Prover must prove 3200 bytes of pubdata at full RISC-V cycle cost with zero native-resource compensation.
7. Repeat across all transactions in the block to drain the operator's proving budget.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L134-138)
```rust
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L141-143)
```rust
    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** api/src/helpers.rs (L426-427)
```rust
    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L352-353)
```rust
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L208-210)
```rust
            eip1559_basefee: U256::from(1000u64),
            pubdata_price: U256::from(0u64),
            native_price: U256::from(10),
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L66-80)
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
```
