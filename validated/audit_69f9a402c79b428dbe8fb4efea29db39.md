### Title
Truncating Floor Division in `native_per_pubdata` Computation Causes Systematic Pubdata Underpayment - (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

### Summary

`native_per_pubdata` is computed using floor division (`wrapping_div`), while `native_per_gas` uses ceiling division (`div_ceil`). This asymmetry causes users to be charged less native resource for pubdata than the operator intends. When `pubdata_price < native_price`, the truncation produces `native_per_pubdata = 0`, completely bypassing pubdata native charging for every transaction that generates pubdata.

### Finding Description

In `validate_and_compute_fee_for_transaction`, the ratio of pubdata price to native price is computed as:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

`wrapping_div` is floor (truncating) integer division. By contrast, `native_per_gas` is computed with ceiling division:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(...)
```

The design intent of `div_ceil` for `native_per_gas` is to ensure the user always has at least enough native resource to cover gas. The same conservative rounding is absent for pubdata.

**Precision-loss case**: When `pubdata_price = k * native_price + r` with `0 < r < native_price`, the computed `native_per_pubdata = k` instead of the true ratio `k + r/native_price`. Every pubdata byte is undercharged by `r` native units.

**Complete-bypass case**: When `pubdata_price < native_price` (e.g., `pubdata_price = 1`, `native_price = 2`), `native_per_pubdata = 0`. This zero value propagates through the entire pubdata charging pipeline:

1. `intrinsic_pubdata_overhead = native_per_pubdata.saturating_mul(intrinsic_pubdata) = 0` — no native withheld upfront for pubdata.
2. `check_enough_resources_for_pubdata` always returns `true` regardless of pubdata generated, because `0 * pubdata_bytes = 0` native required.
3. `get_resources_to_charge_for_pubdata` charges zero native for any amount of pubdata.

The identical truncation is present in the off-chain helper `validate_l2_tx_intrinsic_native_resources` in `api/src/helpers.rs`, so the pre-submission validation mirrors the bootloader's undercharge. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

The downstream charging functions that consume `native_per_pubdata`: [5](#0-4) [6](#0-5) 

### Impact Explanation

Any unprivileged user submitting transactions that generate pubdata (SSTORE writes, contract deployments, L2→L1 logs) is systematically undercharged for native resources. In the complete-bypass case (`pubdata_price < native_price`), users pay zero native for pubdata regardless of how much they generate, up to the block pubdata limit. This breaks the native resource accounting invariant that pubdata costs must be covered by the transaction's native budget, allowing users to impose proving/DA costs on the protocol without paying for them.

### Likelihood Explanation

Medium. The operator controls `pubdata_price` and `native_price`. The truncation is always active whenever `pubdata_price mod native_price != 0`. The complete bypass activates whenever `pubdata_price < native_price`, which is a plausible configuration (e.g., during low-DA-cost periods or misconfiguration). Every transaction generating pubdata is affected without any special attacker action beyond submitting normal transactions.

### Recommendation

Use ceiling division for `native_per_pubdata` to match the conservative rounding applied to `native_per_gas`, ensuring users always pay at least the intended amount:

```rust
// In validation_impl.rs and api/src/helpers.rs
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

### Proof of Concept

Set block context: `native_price = 2`, `pubdata_price = 1`.

1. `native_per_pubdata = floor(1 / 2) = 0`.
2. Submit a transaction that executes 10 SSTORE operations (generating ~320 bytes of pubdata).
3. `intrinsic_pubdata_overhead = 0 * intrinsic_pubdata = 0` — no native withheld.
4. After execution, `check_enough_resources_for_pubdata` computes `0 * 320 = 0` native needed → always passes.
5. `get_resources_to_charge_for_pubdata` charges `0 * 320 = 0` native.
6. The transaction completes with zero native charged for pubdata, despite `pubdata_price = 1 > 0`.

With `native_price = 3`, `pubdata_price = 5`: `native_per_pubdata = floor(5/3) = 1` instead of the true `1.67`. Each pubdata byte is undercharged by `0.67` native units. Over 100,000 pubdata bytes (block limit), the total underpayment is ~66,667 native units per block.

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

**File:** api/src/helpers.rs (L420-424)
```rust
    // native_per_gas = ceil(gas_price / native_price)
    if native_price.is_zero() {
        return Err(());
    }
    let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(())?;
```

**File:** api/src/helpers.rs (L426-427)
```rust
    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** api/src/helpers.rs (L436-442)
```rust
    // Intrinsic pubdata
    let intrinsic_pubdata = calculate_l2_tx_intrinsic_pubdata(authorization_list_num, false);
    let intrinsic_pubdata_overhead = native_per_pubdata.saturating_mul(intrinsic_pubdata);

    let native_limit = native_limit
        .checked_sub(intrinsic_pubdata_overhead)
        .ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L422-434)
```rust
pub fn get_resources_to_charge_for_pubdata<S: EthereumLikeTypes>(
    system: &mut System<S>,
    native_per_pubdata: u64,
    base_pubdata: Option<u64>,
) -> Result<(u64, S::Resources), SystemError> {
    let current_pubdata_spent = system
        .net_pubdata_used()?
        .saturating_sub(base_pubdata.unwrap_or(0));
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
    let native = <S::Resources as zk_ee::system::Resources>::Native::from_computational(native);
    Ok((current_pubdata_spent, S::Resources::from_native(native)))
```
