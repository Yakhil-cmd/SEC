### Title
Integer Division Truncation in `native_per_pubdata` Calculation Allows Free Pubdata Generation - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

The `native_per_pubdata` ratio used to charge transactions for their pubdata footprint is computed with plain floor (truncating) integer division. When `pubdata_price < native_price`, the result truncates to zero, making all pubdata completely free for any transaction sender. This is the direct ZKsync OS analog of the `mulDiv()` rounding-to-zero vulnerability in the reference report.

---

### Finding Description

In `validate_and_compute_fee_for_transaction` (L2 transaction validation), the per-pubdata-byte native cost is computed as:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

`wrapping_div` performs floor (truncating) integer division. When `pubdata_price < native_price`, the result is `0`.

The same truncating division appears in the public API helper:

```rust
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [2](#0-1) 

This `native_per_pubdata = 0` value then flows into every pubdata charging site. In `get_resources_to_charge_for_pubdata`:

```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)   // = 0 when native_per_pubdata == 0
    .ok_or(out_of_native_resources!())?;
``` [3](#0-2) 

And in `create_resources_for_tx`, the intrinsic pubdata overhead is also zeroed:

```rust
let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
``` [4](#0-3) 

The result: `check_enough_resources_for_pubdata` always returns `true` (sufficient resources) because the computed cost is zero, and the transaction is never charged for any pubdata it generates.

By contrast, `native_per_gas` is computed with ceiling division (`div_ceil`) to avoid exactly this truncation:

```rust
u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
    InvalidTransaction::NativeResourcesAreTooExpensive,
))?
``` [5](#0-4) 

The asymmetry is the root cause: `native_per_gas` uses `div_ceil`; `native_per_pubdata` uses `wrapping_div`.

---

### Impact Explanation

When `pubdata_price < native_price` (a realistic market condition — e.g., `pubdata_price = 500`, `native_price = 1000`), `native_per_pubdata` truncates to `0`. Any transaction sender can then:

1. Write to an arbitrary number of storage slots (each slot write generates ~65 bytes of pubdata state diff).
2. Pay zero native resources for all generated pubdata.
3. The operator/sequencer must still publish this pubdata to the settlement layer (L1) and bears the full cost.

This is a **resource accounting bug** causing pubdata cost avoidance. The operator is economically harmed: they pay L1 DA costs that are not recovered from the transaction sender. At scale, this enables pubdata spam / effective DoS of the rollup's DA budget at no cost to the attacker beyond EVM gas.

---

### Likelihood Explanation

The condition `pubdata_price < native_price` is realistic. The `pubdata_price` reflects the cost of one byte of L1 calldata/blob data, while `native_price` reflects the cost of one RISC-V proving cycle. During periods of low L1 gas prices or high proving costs, `pubdata_price` can easily be smaller than `native_price`. The block context values are set by the operator oracle and are not adversarially controlled, but any transaction sender can exploit the condition whenever it holds. [6](#0-5) 

---

### Recommendation

Replace `wrapping_div` with ceiling division (`div_ceil`) for `native_per_pubdata`, consistent with how `native_per_gas` is computed:

```rust
// Before (truncates to 0 when pubdata_price < native_price):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;

// After (rounds up, ensuring at least 1 native unit per pubdata byte when price > 0):
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs` at line 427. This ensures that any non-zero `pubdata_price` always results in at least `1` native unit charged per pubdata byte, preventing complete cost avoidance.

---

### Proof of Concept

**Setup:** Block context with `pubdata_price = 500`, `native_price = 1000`, `eip1559_basefee = 1000`.

**Calculation:**
- `native_per_pubdata = 500.wrapping_div(1000) = 0`
- `native_per_gas = ceil(1000 / 1000) = 1` (normal, non-zero)

**Attack:**
1. Submit an L2 transaction that writes to 100 storage slots (generating ~6,500 bytes of pubdata).
2. `get_resources_to_charge_for_pubdata` computes `native = 6500 * 0 = 0`.
3. `check_enough_resources_for_pubdata` returns `true` (0 ≤ any remaining native).
4. Transaction succeeds; sender pays only EVM gas, zero pubdata cost.
5. Operator must publish 6,500 bytes to L1 at their own expense.

Repeat with many transactions to drain the operator's DA budget at negligible cost to the attacker. [7](#0-6) [8](#0-7)

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

**File:** api/src/helpers.rs (L426-427)
```rust
    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L352-352)
```rust
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L422-435)
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
}
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L445-455)
```rust
pub fn check_enough_resources_for_pubdata<S: EthereumLikeTypes>(
    system: &mut System<S>,
    native_per_pubdata: u64,
    resources: &S::Resources,
    base_pubdata: Option<u64>,
) -> Result<(bool, S::Resources, u64), SystemError> {
    let (pubdata_used, resources_for_pubdata) =
        get_resources_to_charge_for_pubdata(system, native_per_pubdata, base_pubdata)?;
    system_log!(system, "Checking gas for pubdata, resources_for_pubdata: {resources_for_pubdata:?}, resources: {resources:?}\n");
    let enough = resources.has_enough(&resources_for_pubdata);
    Ok((enough, resources_for_pubdata, pubdata_used))
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L193-203)
```rust
impl ZkSpecificPricingMetadata for BlockMetadataFromOracle {
    fn get_pubdata_price(&self) -> U256 {
        self.pubdata_price
    }
    fn native_price(&self) -> U256 {
        self.native_price
    }
    fn get_pubdata_limit(&self) -> u64 {
        self.pubdata_limit
    }
}
```
