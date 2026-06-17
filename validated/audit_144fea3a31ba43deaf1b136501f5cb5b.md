### Title
Truncating Integer Division in `native_per_pubdata` Computation Allows Users to Generate Pubdata for Free - (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

### Summary

The `native_per_pubdata` fee rate is computed using truncating (floor) integer division. When `pubdata_price < native_price`, the result rounds down to zero, causing every subsequent pubdata-charging call to charge the user exactly 0 native tokens per byte of pubdata. Any unprivileged transaction sender can exploit this condition to generate up to the block's full pubdata budget for free, shifting the entire pubdata cost to the operator.

### Finding Description

In `validate_and_compute_fee_for_transaction`, the per-pubdata-byte native cost is derived as:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs, line 142
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

`wrapping_div` performs truncating (floor) integer division. Whenever `pubdata_price < native_price`, the quotient rounds down to `0`. This zero is then stored in the transaction context and propagated to every pubdata-charging site:

```rust
// basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs, lines 430-432
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)   // 0 * anything = 0
    .ok_or(out_of_native_resources!())?;
``` [1](#0-0) [2](#0-1) 

The same truncating division appears in the off-chain helper used by API consumers:

```rust
// api/src/helpers.rs, line 427
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [3](#0-2) 

Contrast this with `native_per_gas`, which deliberately uses **ceiling** division to protect the operator:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs, line 135
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [4](#0-3) 

The asymmetry is the root cause: `native_per_gas` rounds up (operator-protective), while `native_per_pubdata` rounds down (user-beneficial), and can reach exactly zero.

When `native_per_pubdata == 0`, `check_enough_resources_for_pubdata` always returns `enough = true` (zero resources are always available), so the post-execution pubdata check never reverts the transaction regardless of how much pubdata was generated:

```rust
// basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs, lines 451-455
let (pubdata_used, resources_for_pubdata) =
    get_resources_to_charge_for_pubdata(system, native_per_pubdata, base_pubdata)?;
let enough = resources.has_enough(&resources_for_pubdata);
Ok((enough, resources_for_pubdata, pubdata_used))
``` [5](#0-4) 

The block-level pubdata limit (`pubdata_limit`) is still enforced by `check_for_block_limits`, so pubdata generation is bounded per block, but within that limit the cost to the user is zero:

```rust
// basic_bootloader/src/bootloader/block_flow/zk/mod.rs, line 77
} else if !cfg!(feature = "resources_for_tester") && pubdata_used > system.get_pubdata_limit() {
``` [6](#0-5) 

### Impact Explanation

When `pubdata_price < native_price` (a condition that can arise in normal operation, e.g., when the native token appreciates relative to L1 data costs), every L2 transaction pays **zero** native tokens for pubdata. An attacker can:

1. Submit transactions that write to many distinct storage slots, maximising pubdata per transaction.
2. Fill the block's pubdata budget at zero cost.
3. Force the operator to absorb the full L1 data-availability cost without any compensation.

The operator's revenue from pubdata fees is completely denied for every block in which this condition holds. Because the block pubdata limit is a hard cap, the attacker can also crowd out legitimate transactions that would otherwise pay for pubdata, constituting a low-cost denial-of-service against the block's pubdata capacity.

### Likelihood Explanation

The condition `pubdata_price < native_price` is not exotic. Both values are independent block-level parameters supplied by the sequencer. On networks where the native token has high unit value (e.g., ETH-denominated chains) and L1 calldata/blob costs are low, `pubdata_price` can legitimately be smaller than `native_price`. The operator may not realise that this causes the per-pubdata charge to silently collapse to zero rather than to a small positive value. The vulnerability requires no special privilege: any EOA that can submit a transaction can exploit it.

### Recommendation

Replace the truncating division with ceiling division for `native_per_pubdata`, consistent with how `native_per_gas` is computed:

```rust
// validation_impl.rs line 142 — change from:
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// to:
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix to `api/src/helpers.rs` line 427. Alternatively, add an explicit guard that rejects (or treats as zero-pubdata-cost) any block configuration where `pubdata_price < native_price`, and document the invariant prominently.

### Proof of Concept

**Setup:** Deploy a contract that writes to 20 distinct storage slots (generating ~640 bytes of pubdata per call). Configure the block with `native_price = 1000`, `pubdata_price = 999` (a realistic 0.1% spread).

**Computation:**
```
native_per_pubdata = pubdata_price.wrapping_div(native_price)
                   = 999 / 1000
                   = 0   (truncated)
```

**Result:** `get_resources_to_charge_for_pubdata` returns `native = 640 * 0 = 0`. The post-execution check passes unconditionally. The attacker pays only the EVM gas cost for the SSTORE opcodes — zero pubdata fee — while the operator must pay the full L1 data cost for the 640-byte diff.

Repeating across all transactions in a block until `pubdata_limit` is reached denies the operator all pubdata revenue for that block and saturates the pubdata budget, preventing other users from landing pubdata-generating transactions.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L135-137)
```rust
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L141-143)
```rust
    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L451-455)
```rust
    let (pubdata_used, resources_for_pubdata) =
        get_resources_to_charge_for_pubdata(system, native_per_pubdata, base_pubdata)?;
    system_log!(system, "Checking gas for pubdata, resources_for_pubdata: {resources_for_pubdata:?}, resources: {resources:?}\n");
    let enough = resources.has_enough(&resources_for_pubdata);
    Ok((enough, resources_for_pubdata, pubdata_used))
```

**File:** api/src/helpers.rs (L427-427)
```rust
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L77-83)
```rust
    } else if !cfg!(feature = "resources_for_tester") && pubdata_used > system.get_pubdata_limit() {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block pubdata limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockPubdataLimitReached)
```
