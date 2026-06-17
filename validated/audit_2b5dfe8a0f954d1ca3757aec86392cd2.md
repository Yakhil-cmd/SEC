### Title
Floor Division in `native_per_pubdata` Calculation Allows Users to Underpay for Pubdata - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

In `validate_and_compute_fee_for_transaction`, the `native_per_pubdata` ratio is computed using floor division (`wrapping_div`), while the analogous `native_per_gas` ratio is explicitly computed using ceiling division (`div_ceil`). This asymmetry causes the per-byte pubdata cost to be systematically underestimated, allowing any L2 transaction sender to consume more pubdata than they paid for.

---

### Finding Description

In `validation_impl.rs`, two resource ratios are computed from operator-set prices:

```rust
// Line 135: native_per_gas uses CEILING division
let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price))...

// Line 142: native_per_pubdata uses FLOOR division (wrapping_div)
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))...
``` [1](#0-0) 

`native_per_pubdata` is the conversion factor from pubdata bytes to native resource units. It is used in two places:

1. **At transaction start** — to subtract the intrinsic pubdata overhead from the native limit:
   `intrinsic_pubdata_overhead = native_per_pubdata * intrinsic_pubdata` [2](#0-1) 

2. **At transaction end** — to charge for all pubdata consumed during execution:
   `native = current_pubdata_spent * native_per_pubdata` [3](#0-2) 

Because `wrapping_div` truncates toward zero, `native_per_pubdata` is at most `native_price - 1` units smaller than the true ceiling value. This means the user is charged fewer native resource units per pubdata byte than the operator intended.

The `api/src/helpers.rs` helper that mirrors bootloader behavior confirms the asymmetry with explicit comments:

```rust
// native_per_gas = ceil(gas_price / native_price)   ← intentional ceiling
// native_per_pubdata = pubdata_price / native_price  ← no rounding direction stated
``` [4](#0-3) 

The downstream effect flows through `compute_gas_refund`: a smaller `native_used` (because pubdata was undercharged) produces a smaller `delta_gas`, which in turn produces a larger gas refund to the sender and a smaller fee payment to the operator. [5](#0-4) 

---

### Impact Explanation

Every L2 transaction that writes pubdata pays less native resource per pubdata byte than the operator configured. The shortfall is borne by the operator, who must still pay for L1 data publication at the full rate. The maximum underpayment per pubdata byte is `native_price - 1` native units. In the degenerate case where `pubdata_price < native_price`, `native_per_pubdata` rounds to **zero**, making pubdata effectively free for the sender regardless of the operator's `pubdata_price` setting. This is a direct resource accounting loss for the operator/protocol on every affected transaction.

---

### Likelihood Explanation

Every L2 transaction processed through `validate_and_compute_fee_for_transaction` is affected whenever `pubdata_price` is not an exact multiple of `native_price`. This is the common case in production. No special attacker capability is required — any unprivileged transaction sender submitting a normal L2 transaction triggers the path. [6](#0-5) 

---

### Recommendation

Replace `wrapping_div` with `div_ceil` for `native_per_pubdata`, consistent with how `native_per_gas` is computed:

```rust
// Before (floor division — incorrect)
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (ceiling division — correct)
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

The same fix should be applied to the mirror implementation in `api/src/helpers.rs` line 427. [7](#0-6) 

---

### Proof of Concept

Let:
- `pubdata_price = 7`, `native_price = 3`
- Floor: `native_per_pubdata = 2`
- Ceiling: `native_per_pubdata = 3`

A transaction writing 1 000 pubdata bytes is charged `2 000` native units instead of `3 000`. With `native_per_gas = ceil(7/3) = 3`, the `delta_gas` adjustment is `2000/3 = 666` gas instead of `3000/3 = 1000` gas. At `gas_price = 7`, the sender underpays by `(1000 - 666) * 7 = 2338` wei per transaction. Across a high-throughput chain, this compounds into a systematic operator loss.

In the extreme case `pubdata_price = 2`, `native_price = 3`: `native_per_pubdata = 0`, so pubdata is entirely free regardless of how much the sender writes. [8](#0-7)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L46-53)
```rust
pub(crate) fn validate_and_compute_fee_for_transaction<
    S: EthereumLikeTypes,
    Config: BasicBootloaderExecutionConfig,
>(
    system: &mut System<S>,
    transaction: &mut Transaction<S::Allocator>,
    _tracer: &mut impl Tracer<S>,
) -> Result<TxContextForPreAndPostProcessing<S>, TxError>
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L135-143)
```rust
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
    };

    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L352-359)
```rust
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
        Some(val) => val,
        None => P::handle_arithmetic_error(
            system,
            P::native_underflow_error("subtracting pubdata overhead"),
        )?,
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** api/src/helpers.rs (L420-427)
```rust
    // native_per_gas = ceil(gas_price / native_price)
    if native_price.is_zero() {
        return Err(());
    }
    let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(())?;

    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L64-79)
```rust
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
```
