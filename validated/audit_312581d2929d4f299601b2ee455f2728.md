Looking at the external report's vulnerability class — **arithmetic precision/scaling bug in a ratio calculation that truncates to zero, bypassing a resource check** — I need to find an analog in ZKsync OS where a division-based resource accounting calculation silently truncates, allowing an unprivileged caller to bypass payment.

After thorough investigation of the resource accounting pipeline, I found a direct analog.

---

### Title
Floor Division Truncation in `native_per_pubdata` Calculation Allows Pubdata to Be Written for Free When `pubdata_price < native_price` — (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

### Summary

The `native_per_pubdata` ratio — which determines how many native resource units (RISC-V cycles) one byte of pubdata costs — is computed using floor division (`wrapping_div`). When the operator-set `pubdata_price` is less than `native_price`, this truncates to zero. With `native_per_pubdata = 0`, the entire pubdata charging path charges nothing, allowing any unprivileged L2 transaction sender to write pubdata up to the block's `pubdata_limit` without paying any native resources for it. The sequencer/prover bears the full proving cost.

### Finding Description

In `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`, line 142–143:

```rust
// We checked native_price != 0 above
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

`wrapping_div` is integer floor division. When `pubdata_price < native_price` (e.g., `pubdata_price = 999`, `native_price = 1000`), the result is `0`. There is no minimum-value guard, no rejection, and no special-case handling for `native_per_pubdata == 0`.

Contrast this with `native_per_gas`, which explicitly uses **ceiling** division to guarantee at least 1 native unit per gas unit:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
    InvalidTransaction::NativeResourcesAreTooExpensive,
))?
``` [2](#0-1) 

The same floor-division pattern is replicated in `api/src/helpers.rs` in `validate_l2_tx_intrinsic_native_resources`:

```rust
// native_per_pubdata = pubdata_price / native_price
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [3](#0-2) 

**Downstream effect — intrinsic pubdata overhead becomes zero:**

In `create_resources_for_tx`, the intrinsic pubdata overhead is:

```rust
let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
``` [4](#0-3) 

When `native_per_pubdata = 0`, `intrinsic_pubdata_overhead = 0`. No native resources are deducted for intrinsic pubdata.

**Downstream effect — execution pubdata charge becomes zero:**

In `get_resources_to_charge_for_pubdata`:

```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)
    .ok_or(out_of_native_resources!())?;
``` [5](#0-4) 

When `native_per_pubdata = 0`, `native = 0` regardless of how many pubdata bytes were written. The `check_enough_resources_for_pubdata` check trivially passes, and no native resources are charged post-execution.

The `native_price` and `pubdata_price` are independent operator-set block-context fields:

```rust
pub trait ZkSpecificPricingMetadata {
    fn native_price(&self) -> U256;
    fn get_pubdata_price(&self) -> U256;
}
``` [6](#0-5) 

They are deserialized independently from the oracle and have no enforced ordering relationship: [7](#0-6) 

### Impact Explanation

When `pubdata_price < native_price`, `native_per_pubdata` truncates to `0`. Any unprivileged L2 transaction sender can then:

1. Submit transactions that perform many `SSTORE` operations (each producing pubdata).
2. Pay only EVM gas (ergs) for those operations — zero native resources are charged for the pubdata bytes produced.
3. Force the sequencer/prover to bear the full RISC-V proving cost for that pubdata.

The proving cost for pubdata is real and non-trivial. Each pubdata byte requires RISC-V cycles to hash, compress, and prove. With `native_per_pubdata = 0`, the user's payment does not cover this cost. An attacker can repeatedly submit such transactions, draining the sequencer/prover economically without paying the corresponding native resource cost. This is a **resource accounting bug** causing **public funds loss** (operator/prover subsidy).

### Likelihood Explanation

`native_price` (cost per RISC-V cycle, in wei) and `pubdata_price` (cost per pubdata byte, in wei) are set independently by the operator. It is entirely realistic for `pubdata_price < native_price`:

- During low-demand periods, operators may lower `pubdata_price` to attract transactions.
- The two values have different economic meanings and no enforced ordering.
- The operator may set `pubdata_price = 999` and `native_price = 1000` intending to charge ~1 native unit per pubdata byte, but floor division silently produces `native_per_pubdata = 0`.

The attacker does not need to manipulate any privileged role — they only need to observe that `pubdata_price < native_price` in the current block context (publicly visible) and submit a pubdata-heavy transaction.

### Recommendation

Replace `wrapping_div` with `div_ceil` for the `native_per_pubdata` calculation, consistent with how `native_per_gas` is computed:

```rust
// Before (floor division — truncates to 0 when pubdata_price < native_price):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (ceiling division — guarantees at least 1 native unit per pubdata byte when pubdata_price > 0):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs` line 427. This mirrors the fix in the external report, which recommended scaling the ratio to avoid truncation to zero.

### Proof of Concept

**Setup:**
- Block context: `native_price = 1000`, `pubdata_price = 999`, `eip1559_basefee = 1000`
- `native_per_pubdata = 999 / 1000 = 0` (floor division)

**Attack:**
1. Attacker submits an L2 EIP-1559 transaction with `max_fee_per_gas = 1000`, `gas_limit = 10_000_000`, calling a contract that performs 100 `SSTORE` operations (each writing a new non-zero value → ~66 bytes pubdata each → ~6600 bytes total pubdata).
2. During validation: `intrinsic_pubdata_overhead = 0 * L2_TX_INTRINSIC_PUBDATA = 0`. No native deducted for intrinsic pubdata.
3. During execution: `native = 6600 * 0 = 0`. `check_enough_resources_for_pubdata` passes trivially.
4. Post-execution: `get_resources_to_charge_for_pubdata` returns 0 native. Transaction succeeds.
5. Attacker paid only EVM gas for the `SSTORE` operations. Zero native resources were charged for 6600 bytes of pubdata.
6. The prover must prove all 6600 bytes of pubdata at full RISC-V cycle cost, uncompensated.

Repeat across many transactions to drain the operator/prover economically.

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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L352-352)
```rust
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** zk_ee/src/system/metadata/basic_metadata.rs (L60-69)
```rust
pub trait ZkSpecificPricingMetadata {
    /// Price of an unit of native resources.
    fn native_price(&self) -> U256;

    /// Upper bound on total pubdata that can be used by the transaction.
    fn get_pubdata_limit(&self) -> u64;

    /// Price in base token of 1 byte of pubdata.
    fn get_pubdata_price(&self) -> U256;
}
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L272-274)
```rust
        let eip1559_basefee = UsizeDeserializable::from_iter(src)?;
        let pubdata_price = UsizeDeserializable::from_iter(src)?;
        let native_price = UsizeDeserializable::from_iter(src)?;
```
